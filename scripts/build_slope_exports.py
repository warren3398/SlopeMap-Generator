"""Build pre-generated JPEG and Shapefile ZIP exports for the SlopeMap Downloader.
Usage: python -m scripts.build_slope_exports --level province --fid 66
R2 keys: exports/slope/<level>/<fid>/slope.jpg and slope.zip
"""
import argparse, io, json, re, tempfile, zipfile, sqlite3
from pathlib import Path
import geopandas as gpd
import pyogrio
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from app.r2gis import local_file, client, BUCKET, valid_poly, slope_column

COLORS={"A":"#418a41","B":"#89b340","C":"#dde340","D":"#ffe240","E":"#ffa340","F":"#ff0000"}
RANGES={"A":"0–3%","B":"3–8%","C":"8–18%","D":"18–30%","E":"30–50%","F":">50%"}
NAMES={"A":"Level to nearly level","B":"Gently sloping","C":"Sloping","D":"Moderately steep","E":"Steep","F":"Very steep"}
def dataset_fids(path):
    """Read real GeoPackage primary keys; FIDs may start at 1 or contain gaps."""
    with sqlite3.connect(str(path)) as db:
        row=db.execute("SELECT table_name FROM gpkg_geometry_columns LIMIT 1").fetchone()
        if not row:raise ValueError("No geometry layer found in GeoPackage")
        table=row[0]
        quoted='"'+table.replace('"','""')+'"'
        columns=db.execute("PRAGMA table_info("+quoted+")").fetchall()
        pk=next((r[1] for r in columns if r[5]),None)
        if pk is None:raise ValueError("GeoPackage geometry layer lacks a primary key")
        quoted_pk='"'+pk.replace('"','""')+'"'
        return [int(row[0]) for row in db.execute("SELECT "+quoted_pk+" FROM "+quoted+" ORDER BY "+quoted_pk)]

def build_one(level, fid, overwrite=False):
    class Args: pass
    a=Args(); a.level=level; a.fid=fid; a.overwrite=overwrite
    return build_export(a)

def build_export(a):
    if a.fid<0:raise ValueError("Invalid boundary FID")
    key=f"exports/slope/{a.level}/{a.fid}/"
    r2=client()
    if not a.overwrite:
        try:
            r2.head_object(Bucket=BUCKET,Key=key+"slope.jpg")
            r2.head_object(Bucket=BUCKET,Key=key+"slope.zip")
            print("SKIP: exports already exist",key,flush=True);return
        except Exception:pass
    boundary_path=local_file(a.level)
    boundary_frame=pyogrio.read_dataframe(boundary_path,fids=[a.fid])
    if boundary_frame.empty:raise ValueError("Boundary FID not found")
    boundary=valid_poly(boundary_frame.geometry.iloc[0])
    # Resolve the selected administrative unit's actual name. Some province
    # boundary records store the province label in ADM2ALTEN while ADM2_EN is
    # blank; never expose an internal FID as the map's province name.
    row=boundary_frame.iloc[0]
    name_fields={
        "province":("ADM2_EN","ADM2ALTEN","ADM2_NAME","PROVINCE","NAME_1","ADM1_EN"),
        "municipality":("ADM3_EN","ADM3ALTEN","ADM3_NAME","MUNICIPALITY","NAME_2"),
        "barangay":("ADM4_EN","ADM4ALTEN","ADM4_NAME","BARANGAY","NAME_3"),
    }.get(a.level,("ADM2_EN","ADM2ALTEN","ADM3_EN","ADM4_EN","NAME"))
    boundary_name=next((str(row[c]).strip() for c in name_fields if c in boundary_frame.columns and row[c] is not None and str(row[c]).strip() and str(row[c]).strip().lower() not in ("nan","none","null")),None)
    if not boundary_name:
        raise ValueError(f"Administrative name is missing for {a.level} FID {a.fid}; refusing to publish an FID-labelled map")
    boundary_label=boundary_name
    if boundary_frame.crs is None:raise ValueError("Boundary dataset has no CRS")
    slope_path=local_file("slope")
    info=pyogrio.read_info(slope_path);source_crs=info["crs"]
    field=slope_column(info["fields"])
    boundary_source=gpd.GeoSeries([boundary],crs=boundary_frame.crs).to_crs(source_crs).iloc[0]
    candidates=pyogrio.read_dataframe(slope_path,bbox=tuple(boundary_source.bounds))
    if candidates.empty:raise ValueError("No slope polygons intersect this boundary")
    candidates=candidates.to_crs(source_crs)
    # Diagnose source/target spatial alignment before clipping. A few tiny
    # slivers can pass a bbox query even when CRS metadata is incorrect.
    source_bounds=tuple(round(float(v),5) for v in candidates.total_bounds)
    boundary_bounds=tuple(round(float(v),5) for v in boundary_source.bounds)
    source_area=float(candidates.to_crs(6933).geometry.area.sum()/10000)
    print("SPATIAL_QA "+json.dumps({"slope_crs":str(source_crs),"boundary_crs":str(boundary_frame.crs),"boundary_bounds_source_crs":boundary_bounds,"slope_candidates_bounds":source_bounds,"slope_candidates":len(candidates),"slope_candidate_area_ha":round(source_area,3),"boundary_area_ha":round(float(gpd.GeoSeries([boundary_source],crs=source_crs).to_crs(6933).area.iloc[0]/10000),3)}),flush=True)
    candidates=candidates[candidates.geometry.notna() & ~candidates.geometry.is_empty].copy()
    candidates["slope_class"]=candidates[field].astype(str).str.strip().str.upper()
    candidates=candidates[candidates.slope_class.isin(COLORS)].copy()
    candidates["geometry"]=candidates.geometry.intersection(boundary_source)
    candidates=candidates[candidates.geometry.notna() & ~candidates.geometry.is_empty].copy()
    candidates=candidates[candidates.geom_type.isin(["Polygon","MultiPolygon"])].copy()
    if candidates.empty:raise ValueError("No classified slope polygons within selected boundary")
    candidates=candidates.to_crs(6933)
    candidates["area_ha"]=candidates.geometry.area/10000
    candidates=candidates.to_crs(4326)
    boundary_ha=float(gpd.GeoSeries([boundary],crs=boundary_frame.crs).to_crs(6933).area.iloc[0]/10000)
    stats=candidates.groupby("slope_class").area_ha.sum().to_dict()
    total=float(sum(stats.values()))
    coverage_pct=total/boundary_ha*100 if boundary_ha else 0
    print("COVERAGE_QA "+json.dumps({"level":a.level,"fid":a.fid,"name":boundary_label,"boundary_ha":round(boundary_ha,3),"classified_ha":round(total,3),"coverage_pct":round(coverage_pct,2),"features":len(candidates)}),flush=True)
    # A successful export must not silently publish a nearly empty slope map.
    # Report the mismatch before uploading any JPEG or ZIP.
    if coverage_pct<50:
        raise ValueError(f"Insufficient slope coverage for {boundary_label}: {coverage_pct:.2f}% ({total:.2f}/{boundary_ha:.2f} ha). Export not published.")
    candidates["area_ha"]=candidates.area_ha.round(3)
    candidates=candidates[["slope_class","area_ha","geometry"]]
    # Create GIS package in a temporary workspace; shapefile fields remain portable.
    with tempfile.TemporaryDirectory() as td:
        td=Path(td); shp=td/"slope.shp"
        pyogrio.write_dataframe(candidates,shp,driver="ESRI Shapefile",encoding="UTF-8")
        zip_path=td/"slope.zip"
        with zipfile.ZipFile(zip_path,"w",zipfile.ZIP_DEFLATED) as z:
            for ext in (".shp",".shx",".dbf",".prj",".cpg"):
                file=td/("slope"+ext)
                if file.exists():z.write(file,file.name)
        # Upload both artifacts only after map and ZIP generation completes.
        # Layout: map panel + fixed-width information panel; no PDF export.
        # Clean landscape map sheet: generous map frame, aligned information
        # panel, compact legend and readable summary with consistent spacing.
        fig=plt.figure(figsize=(16,10),dpi=180,facecolor="white")
        fig.patch.set_edgecolor("#b8c8bf");fig.patch.set_linewidth(1.0)
        # Fixed printable sheet border and dedicated map-frame border.
        fig.add_artist(plt.Rectangle((.012,.018),.976,.964,transform=fig.transFigure,fill=False,edgecolor="#344a3e",linewidth=1.35,zorder=100))
        fig.add_artist(plt.Rectangle((.035,.075),.635,.825,transform=fig.transFigure,fill=False,edgecolor="#81988a",linewidth=1.15,zorder=90))
        fig.text(.3525,.928,"SLOPE CLASSIFICATION MAP",ha="center",va="center",fontsize=18,fontweight="bold",color="#17231c")
        ax=fig.add_axes([.065,.12,.575,.745])
        panel=fig.add_axes([.70,.085,.265,.83]);panel.axis("off")
        for cls in "ABCDEF":
            part=candidates[candidates.slope_class==cls]
            if not part.empty:
                part.plot(ax=ax,color=COLORS[cls],edgecolor="none",zorder=2)
        boundary_ll=gpd.GeoSeries([boundary],crs=boundary_frame.crs).to_crs(4326)
        boundary_ll.boundary.plot(ax=ax,color="#26352d",linewidth=1.0,zorder=20)
        bounds=boundary_ll.total_bounds
        dx=max(bounds[2]-bounds[0],0.0001);dy=max(bounds[3]-bounds[1],0.0001)
        ax.set_xlim(bounds[0]-dx*.035,bounds[2]+dx*.035)
        ax.set_ylim(bounds[1]-dy*.035,bounds[3]+dy*.035)
        ax.set_aspect("equal");ax.set_axis_off()
        # North arrow anchored to the framed map panel rather than the geometry.
        fig.add_artist(plt.Rectangle((.614,.793),.035,.072,transform=fig.transFigure,fill=False,edgecolor="#26352d",lw=1.1,zorder=95))
        fig.text(.6315,.829,"N\n▲",ha="center",va="center",fontsize=11,fontweight="bold",color="#17231c",zorder=96)
        # Right information panel
        panel.add_patch(plt.Rectangle((0,.91),1,.09,color="#07552f",transform=panel.transAxes))
        panel.text(.5,.955,"SLOPE MAP",color="white",ha="center",va="center",fontsize=19,fontweight="bold",transform=panel.transAxes)
        panel.text(.5,.865,boundary_label.upper(),ha="center",va="center",fontsize=16,fontweight="bold",color="#17231c",transform=panel.transAxes)
        panel.add_patch(plt.Rectangle((0,.79),1,.045,color="#07552f",transform=panel.transAxes))
        panel.text(.5,.812,"SLOPE CLASS LEGEND",color="white",ha="center",va="center",fontsize=11,fontweight="bold",transform=panel.transAxes)
        # Explicit legend rows keep class, range and description aligned.
        for idx,c in enumerate("ABCDEF"):
            yy=.755-idx*.043
            panel.add_patch(plt.Rectangle((.035,yy-.012),.055,.022,color=COLORS[c],transform=panel.transAxes,clip_on=False))
            panel.text(.12,yy,f"{c}   {RANGES[c]}",ha="left",va="center",fontsize=9.2,fontweight="bold",color="#17231c",transform=panel.transAxes)
            panel.text(.49,yy,NAMES[c],ha="left",va="center",fontsize=8.7,color="#34443a",transform=panel.transAxes)
        panel.add_patch(plt.Rectangle((0,.465),1,.043,color="#07552f",transform=panel.transAxes))
        panel.text(.5,.486,"SLOPE CLASS SUMMARY",color="white",ha="center",va="center",fontsize=11,fontweight="bold",transform=panel.transAxes)
        rows=[["CLASS","AREA (HA)","AREA (%)"]]
        for c in "ABCDEF":
            rows.append([c,f"{stats.get(c,0):,.2f}",f"{stats.get(c,0)/total*100 if total else 0:.2f}"])
        rows.append(["TOTAL",f"{total:,.2f}","100.00"])
        table=panel.table(cellText=rows,cellLoc="center",bbox=[0,.16,1,.285],colWidths=[.2,.42,.38])
        table.auto_set_font_size(False);table.set_fontsize(9)
        for (ri,ci),cell in table.get_celld().items():
            cell.set_edgecolor("#cbd8d0");cell.set_linewidth(.7)
            if ri==0:
                cell.set_facecolor("#eaf1ec");cell.set_text_props(weight="bold",color="#17231c")
            elif ri==len(rows)-1:
                cell.set_facecolor("#f1f5f2");cell.set_text_props(weight="bold")
            else:cell.set_facecolor("white")
        panel.add_patch(plt.Rectangle((0,.065),1,.065,color="#07552f",transform=panel.transAxes))
        panel.text(.5,.097,"MAP INFORMATION",color="white",ha="center",va="center",fontsize=10.5,fontweight="bold",transform=panel.transAxes)
        panel.text(.02,.035,f"CRS: WGS 84  |  Classified area: {total:,.2f} ha",fontsize=8.2,color="#34443a",transform=panel.transAxes)
        jpg=td/"slope.jpg";fig.savefig(jpg,format="jpg",dpi=180,facecolor="white");plt.close(fig)
        r2.upload_file(str(zip_path),BUCKET,key+"slope.zip",ExtraArgs={"ContentType":"application/zip"})
        r2.upload_file(str(jpg),BUCKET,key+"slope.jpg",ExtraArgs={"ContentType":"image/jpeg"})
    print("READY",json.dumps({"level":a.level,"fid":a.fid,"name":boundary_label,"features":len(candidates),"boundary_ha":round(boundary_ha,3),"coverage_pct":round(coverage_pct,2),"area_ha":round(total,3),"formats":["jpeg","zip"],"prefix":key}),flush=True)
def main():
    p=argparse.ArgumentParser()
    p.add_argument("--level",choices=["province","municipality","barangay"],required=True)
    p.add_argument("--fid",type=int,default=None)
    p.add_argument("--start",type=int,default=None,help="Start position in sorted GeoPackage FIDs")
    p.add_argument("--limit",type=int,default=1)
    p.add_argument("--overwrite",action="store_true")
    a=p.parse_args()
    if a.fid is not None:
        return build_export(a)
    if a.start is None:raise SystemExit("Specify --fid or --start")
    if a.start<0 or a.limit<1 or a.limit>200:raise SystemExit("Invalid batch range (limit 1–200)")
    # FID order is the exact row index used by /boundaries dropdown.
    path=local_file(a.level)
    fids=dataset_fids(path)
    count=len(fids)
    end=min(a.start+a.limit,count)
    print(f"BATCH {a.level}: positions {a.start}..{end-1} of {count}; actual FIDs {fids[a.start:end]}",flush=True)
    failures=[]
    for fid in fids[a.start:end]:
        print(f"PROCESS {a.level}/{fid}",flush=True)
        try:build_one(a.level,fid,a.overwrite)
        except Exception as exc:
            failures.append({"fid":fid,"error":str(exc)})
            print(f"FAILED {a.level}/{fid}: {type(exc).__name__}: {exc}",flush=True)
    print("BATCH_SUMMARY "+json.dumps({"level":a.level,"start":a.start,"limit":a.limit,"failures":failures}),flush=True)
    if failures:raise SystemExit(f"{len(failures)} exports failed; see BATCH_SUMMARY")
if __name__=="__main__":main()

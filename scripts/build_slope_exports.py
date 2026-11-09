"""Build pre-generated JPEG and Shapefile ZIP exports for the SlopeMap Downloader.
Usage: python -m scripts.build_slope_exports --level province --fid 66
R2 keys: exports/slope/<level>/<fid>/slope.jpg and slope.zip
"""
import argparse, io, json, re, tempfile, zipfile
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
def main():
    p=argparse.ArgumentParser()
    p.add_argument("--level",choices=["province","municipality","barangay"],required=True)
    p.add_argument("--fid",type=int,required=True)
    p.add_argument("--overwrite",action="store_true")
    a=p.parse_args()
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
    if boundary_frame.crs is None:raise ValueError("Boundary dataset has no CRS")
    slope_path=local_file("slope")
    info=pyogrio.read_info(slope_path);source_crs=info["crs"]
    field=slope_column(info["fields"])
    boundary_source=gpd.GeoSeries([boundary],crs=boundary_frame.crs).to_crs(source_crs).iloc[0]
    candidates=pyogrio.read_dataframe(slope_path,bbox=tuple(boundary_source.bounds))
    if candidates.empty:raise ValueError("No slope polygons intersect this boundary")
    candidates=candidates.to_crs(source_crs)
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
    stats=candidates.groupby("slope_class").area_ha.sum().to_dict()
    total=float(sum(stats.values()))
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
        r2.upload_file(str(zip_path),BUCKET,key+"slope.zip",ExtraArgs={"ContentType":"application/zip"})
        # Layout: map panel + fixed-width information panel; no PDF export.
        fig=plt.figure(figsize=(16,10),dpi=180,facecolor="white")
        ax=fig.add_axes([.035,.08,.67,.84])
        panel=fig.add_axes([.735,.08,.235,.84]);panel.axis("off")
        for cls in "ABCDEF":
            part=candidates[candidates.slope_class==cls]
            if not part.empty:part.plot(ax=ax,color=COLORS[cls],edgecolor="none")
        ax.set_aspect("equal");ax.set_axis_off();ax.set_title("SLOPE CLASSIFICATION MAP",fontsize=17,fontweight="bold",pad=12)
        boundary_ll=gpd.GeoSeries([boundary],crs=boundary_frame.crs).to_crs(4326)
        bounds=boundary_ll.total_bounds
        ax.set_xlim(bounds[0],bounds[2]);ax.set_ylim(bounds[1],bounds[3])
        ax.add_patch(plt.Rectangle((.94,.90),.045,.075,transform=ax.transAxes,fill=False,lw=1.2))
        ax.text(.962,.938,"N\n▲",transform=ax.transAxes,ha="center",va="center",fontsize=12,fontweight="bold")
        panel.add_patch(plt.Rectangle((0, .91),1,.09,color="#07552f",transform=panel.transAxes))
        panel.text(.5,.955,"SLOPE MAP",color="white",ha="center",va="center",fontsize=18,fontweight="bold",transform=panel.transAxes)
        panel.text(.5,.875,a.level.title()+" FID "+str(a.fid),ha="center",fontsize=14,fontweight="bold",transform=panel.transAxes)
        panel.add_patch(plt.Rectangle((0,.79),1,.045,color="#07552f",transform=panel.transAxes))
        panel.text(.5,.812,"Legend",color="white",ha="center",va="center",fontsize=13,fontweight="bold",transform=panel.transAxes)
        y=.755
        handles=[Patch(facecolor=COLORS[c],label=f"{c}   {RANGES[c]}   {NAMES[c]}") for c in "ABCDEF"]
        panel.legend(handles=handles,loc="upper left",bbox_to_anchor=(.02,.785),frameon=False,fontsize=9,labelspacing=1.05)
        panel.add_patch(plt.Rectangle((0,.43),1,.045,color="#07552f",transform=panel.transAxes))
        panel.text(.5,.452,"Slope Class Summary",color="white",ha="center",va="center",fontsize=12,fontweight="bold",transform=panel.transAxes)
        rows=[["Class","Area (ha)","Area (%)"]]
        for c in "ABCDEF":rows.append([c,f"{stats.get(c,0):,.2f}",f"{stats.get(c,0)/total*100 if total else 0:.2f}"])
        rows.append(["TOTAL",f"{total:,.2f}","100.00"])
        table=panel.table(cellText=rows,cellLoc="center",bbox=[0,.15,1,.27],colWidths=[.2,.42,.38])
        table.auto_set_font_size(False);table.set_fontsize(9)
        for (ri,ci),cell in table.get_celld().items():
            cell.set_edgecolor("#cbd8d0")
            if ri==0:cell.set_facecolor("#eaf1ec");cell.set_text_props(weight="bold")
            if ri==len(rows)-1:cell.set_text_props(weight="bold")
        panel.add_patch(plt.Rectangle((0,.06),1,.065,color="#07552f",transform=panel.transAxes))
        panel.text(.5,.092,"Map Information",color="white",ha="center",va="center",fontsize=12,fontweight="bold",transform=panel.transAxes)
        panel.text(.03,.035,f"CRS: WGS 84  |  Classified area: {total:,.2f} ha",fontsize=8,transform=panel.transAxes)
        jpg=td/"slope.jpg";fig.savefig(jpg,format="jpg",dpi=180,bbox_inches="tight",pad_inches=.12);plt.close(fig)
        r2.upload_file(str(jpg),BUCKET,key+"slope.jpg",ExtraArgs={"ContentType":"image/jpeg"})
    print("READY",json.dumps({"level":a.level,"fid":a.fid,"features":len(candidates),"area_ha":round(total,3),"formats":["jpeg","zip"],"prefix":key}),flush=True)
if __name__=="__main__":main()

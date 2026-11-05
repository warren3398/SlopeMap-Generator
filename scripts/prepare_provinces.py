"""One-time nationwide slope preprocessing. Run in an environment with existing R2 credentials.

Usage: python -m scripts.prepare_provinces [--start 0] [--limit 88]
Outputs: prepared/provinces/v1/<GeoPackage FID>.geojson in the existing private R2 bucket.
Does not modify original datasets. Safe to resume; existing outputs are skipped.
"""
import argparse
import json
import sqlite3
import time
from shapely import from_wkb
from shapely.geometry import mapping, MultiPolygon
from shapely.ops import transform
from pyproj import Transformer
import pyogrio
from app.r2gis import client, local_file, BUCKET, valid_poly, slope_column

PREFIX="prepared/provinces/v1/"

def ident(name):
    return '"'+str(name).replace('"','""')+'"'

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--start",type=int,default=0)
    parser.add_argument("--limit",type=int,default=1000)
    parser.add_argument("--overwrite",action="store_true")
    args=parser.parse_args()
    slope=local_file("slope")
    provinces=local_file("province")
    info=pyogrio.read_info(slope)
    field=slope_column(info["fields"])
    to_source=Transformer.from_crs(4326,info["crs"],always_xy=True).transform
    to_wgs=Transformer.from_crs(info["crs"],4326,always_xy=True).transform
    to_area=Transformer.from_crs(4326,6933,always_xy=True).transform
    province_info=pyogrio.read_info(provinces)
    # Use actual GeoPackage feature IDs, which need not begin at zero.
    with sqlite3.connect(str(provinces)) as province_db:
        province_layer=province_db.execute("SELECT table_name FROM gpkg_geometry_columns LIMIT 1").fetchone()[0]
        province_cols=province_db.execute("PRAGMA table_info("+ident(province_layer)+")").fetchall()
        province_pk=next((r[1] for r in province_cols if r[5]),"fid")
        province_fids=[int(row[0]) for row in province_db.execute(
            "SELECT "+ident(province_pk)+" FROM "+ident(province_layer)+" ORDER BY "+ident(province_pk))]
    total=len(province_fids)
    storage=client()
    with sqlite3.connect(str(slope)) as db:
        table,geom=db.execute("SELECT table_name,column_name FROM gpkg_geometry_columns LIMIT 1").fetchone()
        rtree="rtree_"+table+"_"+geom
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?",(rtree,)).fetchone():
            raise RuntimeError("Slope GeoPackage lacks RTree spatial index")
        columns=db.execute("PRAGMA table_info("+ident(table)+")").fetchall()
        pk=next((r[1] for r in columns if r[5]),"fid")
        sql=("SELECT t."+ident(geom)+",t."+ident(field)+" FROM "+ident(table)+
             " t JOIN "+ident(rtree)+" r ON t."+ident(pk)+"=r.id"+
             " WHERE r.minx<=? AND r.maxx>=? AND r.miny<=? AND r.maxy>=?")
        for fid in province_fids[args.start:args.start+args.limit]:
            key=PREFIX+str(fid)+".geojson"
            if not args.overwrite:
                try:
                    storage.head_object(Bucket=BUCKET,Key=key)
                    print("SKIP",fid,flush=True)
                    continue
                except Exception as exc:
                    if getattr(exc,"response",{}).get("Error",{}).get("Code") not in ("404","NoSuchKey","NotFound"):
                        raise
            start=time.monotonic()
            frame=pyogrio.read_dataframe(provinces,fids=[fid])
            if frame.empty:continue
            boundary=valid_poly(frame.to_crs(4326).geometry.iloc[0])
            boundary_source=transform(to_source,boundary)
            bounds=boundary_source.bounds
            features=[];stats={}
            cursor=db.execute(sql,(bounds[2],bounds[0],bounds[3],bounds[1]))
            while True:
                rows=cursor.fetchmany(40)
                if not rows:break
                for blob,value in rows:
                    if blob is None:continue
                    envelope=(blob[3]>>1)&7
                    header={0:0,1:32,2:48,3:48,4:64}.get(envelope)
                    if header is None:continue
                    geom_src=valid_poly(from_wkb(bytes(blob)[8+header:]))
                    if geom_src is None or geom_src.is_empty or not geom_src.intersects(boundary_source):continue
                    cut=valid_poly(geom_src.intersection(boundary_source))
                    if cut is None or cut.is_empty:continue
                    if cut.geom_type=="GeometryCollection":
                        polys=[p for p in cut.geoms if p.geom_type=="Polygon"]
                        polys.extend(p for g in cut.geoms if g.geom_type=="MultiPolygon" for p in g.geoms)
                        if not polys:continue
                        cut=MultiPolygon(polys)
                    if cut.geom_type not in ("Polygon","MultiPolygon"):continue
                    cut=transform(to_wgs,cut)
                    area=transform(to_area,cut).area/10000
                    cls=str(value)
                    stats[cls]=stats.get(cls,0)+area
                    # Preserve original clipped polygon topology. Degree-based simplification
                    # at 0.002–0.006 degrees produced large triangular artifacts.
                    display=cut
                    features.append({"type":"Feature","properties":{"slope_class":cls,"area_ha":round(area,3)},"geometry":mapping(display)})
            result={"type":"FeatureCollection","features":features,
                    "statistics":[{"slope_class":k,"area_ha":round(v,3)} for k,v in sorted(stats.items())],
                    "total_classified_ha":round(sum(stats.values()),3),"geometry_generalized":False}
            body=json.dumps(result,separators=(",",":"),allow_nan=False).encode("utf-8")
            # Avoid publishing an incomplete/truncated map as ready.
            storage.put_object(Bucket=BUCKET,Key=key,Body=body,ContentType="application/geo+json")
            print("DONE",fid,len(features),len(body),"bytes",round(time.monotonic()-start,1),"sec",flush=True)
    print("Completed requested province range.",flush=True)

if __name__=="__main__":
    main()

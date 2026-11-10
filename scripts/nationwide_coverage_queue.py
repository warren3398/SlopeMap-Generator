"""Nationwide coverage inventory and verified export queue using existing R2 slope data.
Only candidate administrative boundaries overlapping the existing slope footprint
are tested. A coverage check in build_one is the final publication gate.
"""
import argparse,csv,json
from pathlib import Path
import geopandas as gpd
import pyogrio
from shapely.ops import unary_union
from app.r2gis import local_file,valid_poly
from scripts.build_slope_exports import build_one

FIELDS={
 "province":("ADM2_EN","ADM2ALTEN","PROVINCE","NAME_1"),
 "municipality":("ADM3_EN","MUNICIPALITY","NAME_2"),
 "barangay":("ADM4_EN","BARANGAY","NAME_3"),
}
def main():
 p=argparse.ArgumentParser()
 p.add_argument("--level",required=True,choices=FIELDS)
 p.add_argument("--shard",type=int,default=0)
 p.add_argument("--shards",type=int,default=1)
 p.add_argument("--execute",action="store_true",help="Generate qualifying exports; default is inventory only")
 p.add_argument("--max-build",type=int,default=0,help="0 means no cap")
 a=p.parse_args()
 if a.shards<1 or a.shard<0 or a.shard>=a.shards:raise SystemExit("Invalid shard")
 slope=pyogrio.read_dataframe(local_file("slope"),columns=["Slope_Class"])
 slope=slope[slope["Slope_Class"].astype(str).str.upper().isin(list("ABCDEF"))]
 if slope.empty:raise RuntimeError("Existing source has no A-F polygons")
 # A single union and spatial index avoid reloading the source for every area.
 footprint=unary_union([valid_poly(g) for g in slope.geometry if g is not None and not g.is_empty])
 source=local_file(a.level)
 fields=list(pyogrio.read_info(source)["fields"])
 name_col=next((c for c in FIELDS[a.level] if c in fields),None)
 if not name_col:raise RuntimeError(f"No administrative name field in {a.level}: {fields}")
 boundary=pyogrio.read_dataframe(source,columns=[name_col],fid_as_index=True)
 if boundary.crs!=slope.crs:boundary=boundary.to_crs(slope.crs)
 boundary=boundary.iloc[a.shard::a.shards]
 # Avoid expensive intersections for geometries outside the footprint bbox.
 bounds=footprint.bounds
 rows=[];counts={"READY":0,"PARTIAL":0,"NO SOURCE DATA":0,"FAILED":0,"EXISTING":0}
 from app.r2gis import client,BUCKET
 r2=client() if a.execute else None
 for fid,row in boundary.iterrows():
  geom=valid_poly(row.geometry)
  status="NO SOURCE DATA";pct=0.;error=""
  if geom is not None and not geom.is_empty:
   bx=geom.bounds
   if bx[2]>=bounds[0] and bx[0]<=bounds[2] and bx[3]>=bounds[1] and bx[1]<=bounds[3]:
    try:
     # Area in an equal-area CRS, not degree-squared.
     g=gpd.GeoSeries([geom,footprint.intersection(geom)],crs=boundary.crs).to_crs(6933)
     pct=max(0.,min(100.,100*g.iloc[1].area/g.iloc[0].area)) if g.iloc[0].area else 0.
     status="READY" if pct>=50 else ("PARTIAL" if pct>0 else "NO SOURCE DATA")
    except Exception as exc:status="FAILED";error=str(exc)
  if a.execute and status=="READY":
   key=f"exports/slope/{a.level}/{int(fid)}/"
   try:
    r2.head_object(Bucket=BUCKET,Key=key+"slope.jpg")
    r2.head_object(Bucket=BUCKET,Key=key+"slope.zip")
    status="EXISTING"
   except Exception:
    try:build_one(a.level,int(fid),False)
    except ValueError as exc:
     error=str(exc)
     status="PARTIAL" if "Insufficient slope coverage" in error else ("NO SOURCE DATA" if "No slope polygons" in error or "No classified slope" in error else "FAILED")
    except Exception as exc:status="FAILED";error=str(exc)
  counts[status]+=1
  rows.append({"level":a.level,"fid":int(fid),"name":str(row[name_col]),"coverage_pct":round(pct,3),"status":status,"error":error})
  if len(rows)%100==0:print("PROGRESS "+json.dumps({"level":a.level,"shard":a.shard,"processed":len(rows),"counts":counts}),flush=True)
 out=Path(f"coverage-{a.level}-{a.shard}.csv")
 with out.open("w",newline="",encoding="utf-8") as f:
  w=csv.DictWriter(f,fieldnames=["level","fid","name","coverage_pct","status","error"]);w.writeheader();w.writerows(rows)
 print("COVERAGE_SUMMARY "+json.dumps({"level":a.level,"shard":a.shard,"shards":a.shards,"scanned":len(rows),"counts":counts,"report":str(out)}),flush=True)
 if counts["FAILED"]:raise SystemExit(f"{counts['FAILED']} genuine errors; see report")
if __name__=="__main__":main()

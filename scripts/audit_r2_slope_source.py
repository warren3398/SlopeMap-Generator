"""Read-only audit of actual R2 slope GeoPackage layers, CRS, extent and province overlap."""
import json,sqlite3
import geopandas as gpd
import pyogrio
from app.r2gis import local_file
def main():
 path=local_file("slope")
 provinces=local_file("province")
 layers=pyogrio.list_layers(path)
 print("SOURCE_FILE "+json.dumps({"bytes":path.stat().st_size,"layers":layers.tolist()}),flush=True)
 with sqlite3.connect(path) as db:
  for name, in db.execute("SELECT table_name FROM gpkg_contents"):
   safe='"'+name.replace('"','""')+'"'
   try:print("SOURCE_TABLE "+json.dumps({"table":name,"count":db.execute("SELECT count(*) FROM "+safe).fetchone()[0]}),flush=True)
   except Exception as exc:print("SOURCE_TABLE_ERROR "+repr(exc),flush=True)
 pf=pyogrio.read_dataframe(provinces)
 print("PROVINCE_REFERENCE "+json.dumps({"crs":str(pf.crs),"bounds":pf.total_bounds.tolist(),"count":len(pf)}),flush=True)
 for name,kind in layers:
  if kind!="Polygon" and kind!="MultiPolygon":continue
  info=pyogrio.read_info(path,layer=name)
  df=pyogrio.read_dataframe(path,layer=name)
  print("SLOPE_LAYER "+json.dumps({"layer":name,"crs":str(df.crs),"bounds":df.total_bounds.tolist(),"features":len(df),"fields":list(info["fields"])}),flush=True)
  if df.crs is None:continue
  sf=df.to_crs(4326);pp=pf.to_crs(4326)
  hits=pp.geometry.intersects(sf.geometry.union_all())
  print("PROVINCE_OVERLAP "+json.dumps({"layer":name,"intersecting_provinces":int(hits.sum()),"total_provinces":len(pp)}),flush=True)
if __name__=="__main__":main()

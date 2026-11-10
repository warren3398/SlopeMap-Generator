"""Copernicus GLO-30 public tile discovery. No DEM is downloaded or published.
Usage: python -m scripts.discover_copernicus_dem --south 15 --north 17 --west 120 --east 122
"""
import argparse,csv,json,math,urllib.request,urllib.error
from pathlib import Path

BASE="https://copernicus-dem-30m.s3.amazonaws.com"
def tile(lat,lon):
 ns=f"N{lat:02d}" if lat>=0 else f"S{-lat:02d}"
 ew=f"E{lon:03d}" if lon>=0 else f"W{-lon:03d}"
 stem=f"Copernicus_DSM_COG_10_{ns}_00_{ew}_00_DEM"
 return f"{BASE}/{stem}/{stem}.tif"
def check(url):
 req=urllib.request.Request(url,method="HEAD",headers={"User-Agent":"SlopeMap-DEM-inventory/1.0"})
 try:
  with urllib.request.urlopen(req,timeout=20) as r:return "AVAILABLE",int(r.headers.get("Content-Length","0"))
 except urllib.error.HTTPError as e:
  if e.code==404:return "MISSING",0
  return f"HTTP_{e.code}",0
 except Exception as e:return "ERROR_"+type(e).__name__,0
def main():
 p=argparse.ArgumentParser()
 for k in ("south","north","west","east"):p.add_argument("--"+k,type=float,required=True)
 p.add_argument("--max-tiles",type=int,default=12)
 p.add_argument("--output",default="copernicus_dem_inventory.csv")
 a=p.parse_args()
 if not (-90<=a.south<a.north<=90 and -180<=a.west<a.east<=180):p.error("Invalid bounds")
 coords=[(lat,lon) for lat in range(math.floor(a.south),math.ceil(a.north)) for lon in range(math.floor(a.west),math.ceil(a.east))]
 if len(coords)>a.max_tiles or a.max_tiles>100:p.error("Tile limit exceeded; use smaller bounds (max 100)")
 rows=[]
 for lat,lon in coords:
  url=tile(lat,lon);status,bytes_=check(url)
  row={"south":lat,"west":lon,"status":status,"size_bytes":bytes_,"url":url}
  rows.append(row);print("DEM_TILE "+json.dumps(row),flush=True)
 with Path(a.output).open("w",newline="",encoding="utf-8") as f:
  w=csv.DictWriter(f,fieldnames=["south","west","status","size_bytes","url"]);w.writeheader();w.writerows(rows)
 print("DEM_INVENTORY_SUMMARY "+json.dumps({"checked":len(rows),"available":sum(x["status"]=="AVAILABLE" for x in rows),"missing":sum(x["status"]=="MISSING" for x in rows),"errors":sum(x["status"] not in ("AVAILABLE","MISSING") for x in rows),"output":a.output}),flush=True)
if __name__=="__main__":main()

"""Prepare gap-free raster slope tiles from the original slope GeoPackage.

Pilot: python -m scripts.prepare_slope_tiles --province-fid 66 --min-zoom 7 --max-zoom 11
Tiles are drawn from UNSIMPLIFIED source polygons. They never change area statistics.
"""
import argparse, io, json, math, sqlite3
from PIL import Image, ImageDraw
from shapely import from_wkb
from shapely.geometry import box
from shapely.ops import transform
from pyproj import Transformer
import pyogrio
from app.r2gis import local_file, client, BUCKET, valid_poly, slope_column

PALETTE={"A":(65,138,65,255),"B":(137,179,64,255),"C":(221,227,64,255),
         "D":(255,226,64,255),"E":(255,163,64,255),"F":(255,0,0,255)}
WORLD=20037508.342789244
def tile_range(bounds,z):
    n=2**z
    x0=max(0,int(math.floor((bounds[0]+WORLD)/(2*WORLD)*n)))
    x1=min(n-1,int(math.floor((bounds[2]+WORLD)/(2*WORLD)*n)))
    y0=max(0,int(math.floor((WORLD-bounds[3])/(2*WORLD)*n)))
    y1=min(n-1,int(math.floor((WORLD-bounds[1])/(2*WORLD)*n)))
    return x0,x1,y0,y1
def tile_box(x,y,z):
    w=2*WORLD/(2**z)
    return box(-WORLD+x*w,WORLD-(y+1)*w,-WORLD+(x+1)*w,WORLD-y*w)
def paint(draw,geom,extent,color):
    if geom.is_empty:return
    if geom.geom_type=="Polygon": polygons=[geom]
    elif geom.geom_type=="MultiPolygon":polygons=geom.geoms
    elif geom.geom_type=="GeometryCollection":
        for g in geom.geoms:paint(draw,g,extent,color)
        return
    else:return
    x0,y0,x1,y1=extent
    def points(ring):
        return [((px-x0)*256/(x1-x0),(y1-py)*256/(y1-y0)) for px,py in ring.coords]
    for p in polygons:
        draw.polygon(points(p.exterior),fill=color)
        for hole in p.interiors:draw.polygon(points(hole),fill=(0,0,0,0))
def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--province-fid",type=int,required=True)
    ap.add_argument("--min-zoom",type=int,default=7)
    ap.add_argument("--max-zoom",type=int,default=11)
    ap.add_argument("--max-tiles",type=int,default=3000)
    args=ap.parse_args()
    if args.min_zoom<0 or args.max_zoom>14 or args.min_zoom>args.max_zoom:raise ValueError("Invalid zoom range")
    slope=local_file("slope");province=local_file("province")
    src=pyogrio.read_info(slope);field=slope_column(src["fields"])
    frame=pyogrio.read_dataframe(province,fids=[args.province_fid]).to_crs(3857)
    if frame.empty:raise ValueError("Province FID not found")
    boundary=valid_poly(frame.geometry.iloc[0])
    from_merc=Transformer.from_crs(3857,src["crs"],always_xy=True).transform
    to_merc=Transformer.from_crs(src["crs"],3857,always_xy=True).transform
    r2=client()
    prefix=f"tiles/provinces/v1/{args.province_fid}/"
    count=0
    with sqlite3.connect(str(slope)) as db:
        table,geom=db.execute("SELECT table_name,column_name FROM gpkg_geometry_columns LIMIT 1").fetchone()
        cols=db.execute('PRAGMA table_info("'+table.replace('"','""')+'")').fetchall()
        pk=next((c[1] for c in cols if c[5]),"fid")
        def q(s):return '"'+s.replace('"','""')+'"'
        sql=(f"SELECT t.{q(geom)},t.{q(field)} FROM {q(table)} t JOIN {q('rtree_'+table+'_'+geom)} r ON t.{q(pk)}=r.id "
             "WHERE r.minx<=? AND r.maxx>=? AND r.miny<=? AND r.maxy>=?")
        for z in range(args.min_zoom,args.max_zoom+1):
            xa,xb,ya,yb=tile_range(boundary.bounds,z)
            for x in range(xa,xb+1):
                for y in range(ya,yb+1):
                    tile=tile_box(x,y,z)
                    if not boundary.intersects(tile):continue
                    count+=1
                    if count>args.max_tiles:raise RuntimeError("Tile limit exceeded; use smaller zoom range")
                    src_bounds=transform(from_merc,tile).bounds
                    image=Image.new("RGBA",(256,256),(0,0,0,0))
                    draw=ImageDraw.Draw(image)
                    rows=db.execute(sql,(src_bounds[2],src_bounds[0],src_bounds[3],src_bounds[1]))
                    painted=0
                    for blob,value in rows:
                        if blob is None:continue
                        header={0:0,1:32,2:48,3:48,4:64}.get((blob[3]>>1)&7)
                        if header is None:continue
                        g=valid_poly(from_wkb(bytes(blob)[8+header:]))
                        if g is None or g.is_empty:continue
                        cls=str(value).strip().upper()
                        if cls not in PALETTE:continue
                        projected=transform(to_merc,g)
                        if not projected.intersects(tile):continue
                        clipped=projected.intersection(tile).intersection(boundary)
                        if clipped.is_empty:continue
                        paint(draw,clipped,tile.bounds,PALETTE[cls]);painted+=1
                    if painted:
                        out=io.BytesIO();image.save(out,format="PNG",optimize=True)
                        r2.put_object(Bucket=BUCKET,Key=f"{prefix}{z}/{x}/{y}.png",Body=out.getvalue(),ContentType="image/png")
                    if count%50==0:print("TILES",count,"zoom",z,flush=True)
    metadata={"province_fid":args.province_fid,"min_zoom":args.min_zoom,"max_zoom":args.max_zoom,
              "bounds_3857":list(boundary.bounds),"tiles_checked":count,"rendering":"original-unsimplified"}
    r2.put_object(Bucket=BUCKET,Key=prefix+"metadata.json",Body=json.dumps(metadata).encode(),ContentType="application/json")
    print("READY",json.dumps(metadata),flush=True)
if __name__=="__main__":main()

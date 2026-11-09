"""Audit source slope GeoPackage extent and spatial index without generating exports."""
import json, sqlite3
from app.r2gis import local_file, slope_column
import pyogrio

def main():
    path=local_file("slope")
    info=pyogrio.read_info(path)
    print("SLOPE_LAYER_INFO "+json.dumps({"features":int(info["features"]),"crs":str(info["crs"]),"fields":[str(x) for x in info["fields"]],"slope_field":slope_column(info["fields"])}),flush=True)
    with sqlite3.connect(str(path)) as db:
        contents=db.execute("SELECT table_name,data_type,srs_id,min_x,min_y,max_x,max_y FROM gpkg_contents").fetchall()
        geom=db.execute("SELECT table_name,column_name,srs_id FROM gpkg_geometry_columns").fetchall()
        print("GPKG_CONTENTS "+json.dumps(contents),flush=True)
        print("GPKG_GEOMETRY_COLUMNS "+json.dumps(geom),flush=True)
        for table,column,_ in geom:
            rtree="rtree_"+table+"_"+column
            exists=db.execute("SELECT 1 FROM sqlite_master WHERE name=?",(rtree,)).fetchone()
            if exists:
                stats=db.execute("SELECT COUNT(*),MIN(minx),MIN(miny),MAX(maxx),MAX(maxy) FROM "+'"'+rtree+'"').fetchone()
                print("RTREE_EXTENT "+json.dumps({"table":table,"rtree":rtree,"stats":stats}),flush=True)
            else:
                print("RTREE_MISSING "+table,flush=True)
    # Compare against the affected province and neighboring provinces using actual boundary FIDs.
    provinces=local_file("province")
    pinfo=pyogrio.read_info(provinces)
    print("PROVINCE_LAYER_INFO "+json.dumps({"features":int(pinfo["features"]),"crs":str(pinfo["crs"]),"fields":[str(x) for x in pinfo["fields"]]}),flush=True)
    name=next((c for c in ("ADM2_EN","PROVINCE","PROV_NAME","NAME_2") if c in pinfo["fields"]),None)
    if name:
        frame=pyogrio.read_dataframe(provinces,columns=[name],read_geometry=False,fid_as_index=True)
        for fid,row in frame.iterrows():
            if str(row[name]).strip().casefold()=="ilocos norte":
                geom=pyogrio.read_dataframe(provinces,fids=[int(fid)])
                print("ILOCOS_NORTE_BOUNDARY "+json.dumps({"fid":int(fid),"name":str(row[name]),"bounds":list(map(float,geom.total_bounds)),"crs":str(geom.crs)}),flush=True)
                break
if __name__=="__main__":main()

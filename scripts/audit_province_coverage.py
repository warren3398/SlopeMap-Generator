"""Report which existing province boundaries are actually covered by the existing slope layer.
Does not download or create a DEM and does not modify source data or R2 exports.
"""
import csv, json
from pathlib import Path
import geopandas as gpd
import pyogrio
from shapely.ops import unary_union
from app.r2gis import local_file, slope_column, valid_poly

def pick(fields, names):
    return next((x for x in names if x in fields),None)

def main():
    sp=local_file("slope"); pp=local_file("province")
    si=pyogrio.read_info(sp); pi=pyogrio.read_info(pp)
    sf=pick(list(si["fields"]),["Slope_Class","SlopeClass","cat","Slope_Class"])
    pf=pick(list(pi["fields"]),["ADM2_EN","PROVINCE","PROV_NAME","NAME_2"])
    if not sf or not pf: raise RuntimeError("Missing slope class or province name field")
    slopes=pyogrio.read_dataframe(sp,columns=[sf])
    slopes=slopes[slopes.geometry.notna() & ~slopes.geometry.is_empty].copy()
    slopes["cls"]=slopes[sf].astype(str).str.strip().str.upper()
    slopes=slopes[slopes.cls.isin(list("ABCDEF"))].to_crs(6933)
    # Dissolve existing classified coverage to avoid double-counting overlaps.
    coverage=unary_union([valid_poly(g) for g in slopes.geometry if g is not None and not g.is_empty])
    provinces=pyogrio.read_dataframe(pp,columns=[pf],fid_as_index=True).to_crs(6933)
    rows=[]
    for fid,row in provinces.iterrows():
        geom=valid_poly(row.geometry)
        if geom is None or geom.is_empty: continue
        area=geom.area/10000
        overlap=coverage.intersection(geom).area/10000
        pct=overlap/area*100 if area else 0
        rec={"fid":int(fid),"province":str(row[pf]),"boundary_ha":round(area,3),"existing_slope_ha":round(overlap,3),"coverage_pct":round(pct,3),"status":"READY" if pct>=50 else ("PARTIAL" if pct>0 else "NO SOURCE DATA")}
        rows.append(rec);print("PROVINCE_COVERAGE "+json.dumps(rec),flush=True)
    out=Path("province_slope_coverage.csv")
    with out.open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    print("COVERAGE_SUMMARY "+json.dumps({"provinces":len(rows),"ready":sum(x["status"]=="READY" for x in rows),"partial":sum(x["status"]=="PARTIAL" for x in rows),"no_source_data":sum(x["status"]=="NO SOURCE DATA" for x in rows),"csv":str(out)}),flush=True)
if __name__=="__main__":main()

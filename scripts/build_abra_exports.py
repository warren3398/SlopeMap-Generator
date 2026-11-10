"""Pre-generate all Abra municipality and barangay slope exports from existing R2 data.

Run with --level municipality|barangay --shard N --shards 4.
Only records with >=50% classified coverage are published. Others are reported
as unavailable without manufacturing slope data.
"""
import argparse,json
import pyogrio
from app.r2gis import local_file
from scripts.build_slope_exports import dataset_fids,build_one

def scoped_fids(level):
    municipalities=local_file("municipality")
    m=pyogrio.read_dataframe(municipalities,columns=["GID_2","NAME_1","NAME_2"],read_geometry=False,fid_as_index=True)
    abra=m[m["NAME_1"].astype(str).str.strip().str.casefold()=="abra"]
    if abra.empty:raise RuntimeError("No Abra municipalities found in municipality dataset")
    if level=="municipality":return [int(fid) for fid in abra.index]
    path=local_file("barangay")
    fields=list(pyogrio.read_info(path)["fields"])
    if "GID_2" not in fields:raise RuntimeError("Barangay dataset has no GID_2 parent field")
    b=pyogrio.read_dataframe(path,columns=["GID_2"],read_geometry=False,fid_as_index=True)
    parent_ids=set(abra["GID_2"].astype(str).str.strip())
    return [int(fid) for fid in b[b["GID_2"].astype(str).str.strip().isin(parent_ids)].index]

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--level",required=True,choices=["municipality","barangay"])
    p.add_argument("--shard",type=int,required=True)
    p.add_argument("--shards",type=int,default=4)
    a=p.parse_args()
    if a.shards<1 or not 0<=a.shard<a.shards:raise SystemExit("Invalid shard")
    fids=sorted(scoped_fids(a.level))
    selected=fids[a.shard::a.shards]
    print("ABRA_SCOPE "+json.dumps({"level":a.level,"total":len(fids),"shard":a.shard,"shards":a.shards,"selected":len(selected)}),flush=True)
    successes=[];failures=[]
    for i,fid in enumerate(selected,1):
        print(f"PROCESS {i}/{len(selected)} {a.level}/{fid}",flush=True)
        try:
            build_one(a.level,fid,False)
            successes.append(fid)
        except Exception as exc:
            failures.append({"fid":fid,"error":str(exc)})
            print(f"UNAVAILABLE {a.level}/{fid}: {type(exc).__name__}: {exc}",flush=True)
    print("ABRA_EXPORT_SUMMARY "+json.dumps({"level":a.level,"shard":a.shard,"total_selected":len(selected),"completed":len(successes),"unavailable":len(failures),"failures":failures}),flush=True)
    if failures:print("NOTE: Unavailable records are not published; check coverage and errors above.",flush=True)

if __name__=="__main__":main()

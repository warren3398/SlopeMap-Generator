"""Discover uploaded provincial slope packages, including multipart Palawan."""
import hashlib
import json
import re
import threading
from pathlib import Path

from . import r2gis
from .memory_utils import release_memory

LOCK=threading.Lock()
PATTERN=re.compile(r'^slope/(.+?)_slope(?:_(north|south))?\.gpkg$',re.IGNORECASE)


def group_name(value):
    from .downloader import canonical
    name=canonical(value)
    if name in ('maguindanao-del-norte','maguindanao-del-sur'):return 'maguindanao'
    return name


def catalog():
    groups={}
    for page in r2gis.client().get_paginator('list_objects_v2').paginate(Bucket=r2gis.BUCKET,Prefix='slope/'):
        for obj in page.get('Contents',[]):
            key=obj['Key']
            match=PATTERN.fullmatch(key)
            if not match or key.lower()=='slope/philippines_master_slope.gpkg' or obj.get('Size',0)<=0:continue
            name,part=match.groups()
            group=group_name(name)
            groups.setdefault(group,[]).append({'Key':key,'ETag':obj.get('ETag'),'LastModified':str(obj.get('LastModified','')),'Size':obj['Size'],'group':group,'part':part.lower() if part else ''})
    result={}
    for group,files in groups.items():
        whole=[source for source in files if not source['part']]
        if whole:
            if len(whole)!=1:raise ValueError('Ambiguous uploaded slope filenames for '+group+'.')
            result[group]=whole
        else:result[group]=sorted(files,key=lambda source:source['Key'].lower())
    return result


def source_groups(level,fid):
    from . import downloader as service
    if level=='region':
        return [group for province_id,_ in service.province_rows(fid) for group in source_groups('province',str(province_id))]
    path,_=service.object_file(level)
    table=service.attributes(str(path))
    if int(fid) not in table.index:raise ValueError('Selected boundary was not found.')
    row=table.loc[int(fid)]
    if level!='province' and row.get('NAME_1') is not None:
        return [group_name(row['NAME_1'])]
    name=service.actual_name(row,'province')
    group=group_name(name)
    # Independent cities are stored inside their GADM parent province package.
    city_parents={'isabela-city':'basilan','cotabato-city':'maguindanao'}
    if group in city_parents:return [city_parents[group]]
    if str(name).lower().startswith('city of ') or group.startswith('ncr') or 'district' in group:
        city_path,_=service.object_file('municipality')
        children=service.spatial_children('municipality',city_path,'province:'+str(fid))
        city_table=service.attributes(str(city_path))
        parents={group_name(city_table.loc[int(child['code'].split(':')[1])]['NAME_1']) for child in children if 'NAME_1' in city_table}
        if parents:return sorted(parents)
    return [group]


def select_sources(level,fid):
    groups=catalog()
    wanted=source_groups(level,fid)
    # Regional catalogs contain independent city records in addition to provinces.
    aliases={'isabela-city':'basilan','cotabato-city':'maguindanao'}
    wanted=[aliases.get(group,group) for group in wanted]
    missing=sorted({name for name in wanted if name not in groups})
    if missing:raise ValueError('Uploaded slope data is not available for: '+', '.join(missing)+'.')
    sources=[]
    for group in sorted(set(wanted)):
        files=groups[group]
        if group=='palawan' and any(source['part'] for source in files):
            parts={source['part'] for source in files}
            if parts!={'north','south'} or len(files)!=2:raise ValueError('Both Palawan north and south GeoPackages are required for complete coverage.')
        sources.extend(files)
    if not sources:raise ValueError('No uploaded provincial slope packages matched this selection.')
    return sources


def local_source(source):
    from . import downloader as service
    stamp=json.dumps([source['Key'],source.get('ETag'),source.get('LastModified')])
    digest=hashlib.sha256(stamp.encode()).hexdigest()
    path=service.ROOT/'sources'/('slope-'+digest+'.gpkg')
    if not path.exists():
        with LOCK:
            if not path.exists():
                path.parent.mkdir(parents=True,exist_ok=True)
                staging=path.with_suffix('.part')
                try:
                    r2gis.client().download_file(r2gis.BUCKET,source['Key'],str(staging),Config=service.TRANSFER)
                    if not staging.stat().st_size:raise ValueError('Uploaded slope package is empty.')
                    staging.replace(path)
                finally:staging.unlink(missing_ok=True)
    return path

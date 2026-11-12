"""Bounded-memory vector exports; geometry is never simplified for GIS downloads."""
from pathlib import Path
import tempfile
import re
import json
import zipfile

import geopandas as gpd
import pandas as pd
import numpy as np
import pyogrio
import shapely
from .memory_utils import release_memory
from .source_overlap import GeometrySeen,subtract_previous_parts
from boto3.s3.transfer import TransferConfig

BATCH_SIZE = 128
TRANSFER = TransferConfig(max_concurrency=2, max_io_queue=4, io_chunksize=256*1024)


class AreaBatches:
    """Return each batch without retaining a second frame in a suspended generator."""
    def __init__(self,path,boundary=None,columns=None):
        self.path=path
        self.info=pyogrio.read_info(path)
        if not self.info['crs']:
            raise ValueError('Source data has no CRS.')
        self.columns=columns
        self.batch_size=BATCH_SIZE
        self.offset=0
        self.clip=None
        self.bbox=None
        if boundary is not None:
            clip=boundary.to_crs(self.info['crs'])
            self.bbox=tuple(clip.total_bounds)
            repaired=shapely.make_valid(clip.geometry.array,method='structure',keep_collapsed=False)
            self.clip=shapely.union_all(repaired)

    def __iter__(self):return self

    def __next__(self):
        while True:
            offset=self.offset
            frame=pyogrio.read_dataframe(self.path,columns=self.columns,bbox=self.bbox,skip_features=offset,max_features=self.batch_size,fid_as_index=True)
            if frame.empty:
                break
            self.offset+=len(frame)
            frame=frame[frame.geometry.notna() & ~frame.geometry.is_empty].copy()
            if self.clip is not None and not frame.empty:
                for index in frame.index[~frame.geometry.is_valid]:
                    frame.at[index,frame.geometry.name]=shapely.make_valid(frame.at[index,frame.geometry.name],method='structure',keep_collapsed=False)
                frame=gpd.clip(frame,self.clip,keep_geom_type=True)
            frame=frame[frame.geom_type.isin(['Polygon','MultiPolygon'])]
            if not frame.empty:
                return frame
        raise StopIteration


def area_batches(path,boundary=None,columns=None):
    return AreaBatches(path,boundary,columns)


def schema_for(infos):
    fields = {}
    for info in infos:
        for name, dtype in zip(info['fields'], info['dtypes']):
            fields.setdefault(str(name), []).append(np.dtype(dtype).kind)
    return {name: ('Int64' if set(kinds) <= {'i'} else 'float64' if set(kinds) <= {'i','u','f'} and 'u' not in kinds else 'string') for name, kinds in fields.items()}


def build_geopackage(storage, bucket, sources, layer, boundary, target):
    from .export_styles import STYLES, find_field
    from .raster_exports import class_code
    palette = {code for code, _, _ in STYLES[layer]}
    target = Path(target)
    target.unlink(missing_ok=True)
    written = 0
    with tempfile.TemporaryDirectory() as scratch:
        paths, infos = [], []
        for index, source in enumerate(sources):
            path = Path(scratch) / f'{index}.gpkg'
            storage.download_file(bucket, source['Key'], str(path), Config=TRANSFER)
            paths.append(path)
            infos.append(pyogrio.read_info(path))
        schema = schema_for(infos)
        seen=GeometrySeen(Path(scratch)/'seen.sqlite')
        split_groups={source.get('group') for source in sources if source.get('part')}
        # Derived provenance names are stable; retain original attributes too.
        derived = {'slope_code': 'string', 'clip_ha': 'float64', 'source_fid': 'string', 'source_key':'string','source_part':'string','source_group':'string'}
        schema.update(derived)
        total_ha=0.0
        statistics={code:0.0 for code in palette}
        try:
            for path, info, source in zip(paths, infos, sources):
                try:
                    class_field = find_field(pd.DataFrame(columns=info['fields']), layer)
                except ValueError:
                    class_field = None
                for frame in area_batches(path, boundary):
                    source_ids = frame.index.astype(str)
                    codes = frame[class_field].map(class_code) if class_field else pd.Series(None, index=frame.index)
                    frame['slope_code']=codes
                    frame['source_fid']=source_ids
                    frame=frame[frame.slope_code.isin(palette)].copy()
                    if frame.empty:continue
                    frame=frame.to_crs(4326)
                    keep=[]
                    for index,row in frame.iterrows():
                        geometry=row.geometry
                        if seen.contains(geometry,row.slope_code):continue
                        if source.get('group') in split_groups:
                            geometry=subtract_previous_parts(geometry,target,source.get('group',''),source['Key'])
                        if geometry.is_empty or seen.duplicate(geometry,row.slope_code):continue
                        frame.at[index,frame.geometry.name]=geometry
                        keep.append(index)
                    frame=frame.loc[keep].copy()
                    if frame.empty:continue
                    frame['source_key']=source['Key']
                    frame['source_part']=source.get('part','')
                    frame['source_group']=source.get('group','')
                    frame['clip_ha']=frame.to_crs(6933).geometry.area/10000
                    total_ha+=float(frame.clip_ha.sum())
                    for code,value in frame.groupby('slope_code').clip_ha.sum().items():statistics[code]+=float(value)
                    for name, dtype in schema.items():
                        if name not in frame:
                            frame[name] = pd.NA
                        if dtype == 'string':
                            frame[name] = frame[name].astype('string')
                        else:
                            frame[name] = pd.to_numeric(frame[name]).astype(dtype)
                    frame = frame[[*schema, frame.geometry.name]].to_crs(4326)
                    frame.to_file(target, driver='GPKG', layer='map', index=False, mode='a' if written else 'w', promote_to_multi=True)
                    written += len(frame)
                    del frame
                    release_memory(full=False)
        finally:seen.close()
    if not written:
        raise ValueError('No classified slope data is available in the selected area.')
    return {'feature_count':written,'total_ha':total_ha,'statistics':statistics}


def shapefile_from_geopackage(gpkg, target, folder):
    info = pyogrio.read_info(gpkg)
    renamed, used = {}, {'geometry'}
    for column in info['fields']:
        base = re.sub(r'[^A-Za-z0-9_]', '_', str(column))[:10] or 'field'
        name, index = base, 1
        while name.lower() in used:
            suffix = str(index)
            name = base[:10-len(suffix)] + suffix
            index += 1
        renamed[column] = name
        used.add(name.lower())
    shape = Path(folder) / 'shapefile'
    shape.mkdir(exist_ok=True)
    for path in shape.glob('map.*'):
        path.unlink()
    written = 0
    for frame in area_batches(gpkg):
        pyogrio.write_dataframe(frame.rename(columns=renamed).reset_index(drop=True),shape / 'map.shp',driver='ESRI Shapefile',encoding='UTF-8',append=bool(written))
        written += len(frame)
        del frame
        release_memory(full=False)
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in shape.glob('map.*'):
            archive.write(path, path.name)
        archive.writestr('fields.json', json.dumps(renamed, indent=2))

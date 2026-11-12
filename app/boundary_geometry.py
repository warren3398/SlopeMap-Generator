"""Read a selected GeoPackage geometry without an additional OGR geometry copy."""
import sqlite3
from contextlib import closing
import geopandas as gpd
import pyogrio
import shapely
from .source_overlap import ident


def read_geometry(path,fid):
    with closing(sqlite3.connect(f'file:{path}?mode=ro',uri=True)) as database:
        table,column=database.execute('SELECT table_name,column_name FROM gpkg_geometry_columns LIMIT 1').fetchone()
        primary=next(row[1] for row in database.execute('PRAGMA table_info('+ident(table)+')') if row[5])
        predicate=' FROM '+ident(table)+' WHERE '+ident(primary)+'=?'
        row=database.execute('SELECT substr('+ident(column)+',1,8)'+predicate,(int(fid),)).fetchone()
        if not row or not row[0]:raise ValueError('Selected administrative boundary was not found.')
        envelope=(row[0][3]>>1)&7
        size={0:0,1:32,2:48,3:48,4:64}.get(envelope)
        if size is None:raise ValueError('Unsupported boundary geometry encoding.')
        # SQLite strips the GPKG header, so Python holds only one WKB buffer.
        geometry=shapely.from_wkb(database.execute('SELECT substr('+ident(column)+',?)'+predicate,(9+size,int(fid))).fetchone()[0])
    return gpd.GeoDataFrame(geometry=[geometry],crs=pyogrio.read_info(path)['crs'])

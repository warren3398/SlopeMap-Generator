"""Remove duplicate/overlapping parts without retaining province geometry in RAM."""
import hashlib
import sqlite3
import shapely
from contextlib import closing


def ident(name):return '"'+name.replace('"','""')+'"'


def polygonal(geometry):
    if geometry.geom_type in ('Polygon','MultiPolygon'):return geometry
    parts=[]
    if geometry.geom_type=='GeometryCollection':
        for piece in shapely.get_parts(geometry):
            if piece.geom_type=='Polygon':parts.append(piece)
            elif piece.geom_type=='MultiPolygon':parts.extend(piece.geoms)
    return shapely.MultiPolygon(parts)


def subtract_previous_parts(geometry,target,group,key):
    if not target.exists() or geometry.is_empty:return geometry
    with closing(sqlite3.connect(f'file:{target}?mode=ro',uri=True)) as database:
        table,column=database.execute('SELECT table_name,column_name FROM gpkg_geometry_columns LIMIT 1').fetchone()
        pk=next(row[1] for row in database.execute('PRAGMA table_info('+ident(table)+')') if row[5])
        tree='rtree_'+table+'_'+column
        left,bottom,right,top=geometry.bounds
        sql=('SELECT t.'+ident(column)+' FROM '+ident(table)+' t JOIN '+ident(tree)+' r ON t.'+ident(pk)+'=r.id '
             'WHERE r.minx<=? AND r.maxx>=? AND r.miny<=? AND r.maxy>=? AND t.source_group=? AND t.source_key<>?')
        cursor=database.execute(sql,(right,left,top,bottom,group,key))
        for (blob,) in cursor:
            envelope=(blob[3]>>1)&7
            header={0:0,1:32,2:48,3:48,4:64}.get(envelope)
            if header is None:raise ValueError('Unsupported cached geometry encoding.')
            previous=shapely.from_wkb(bytes(blob)[8+header:])
            if geometry.intersects(previous):
                geometry=polygonal(geometry.difference(previous))
                if geometry.is_empty:break
    return geometry


class GeometrySeen:
    def __init__(self,path):
        self.database=sqlite3.connect(path)
        self.database.execute('CREATE TABLE IF NOT EXISTS seen (digest BLOB PRIMARY KEY) WITHOUT ROWID')
    def contains(self,geometry,code):
        digest=hashlib.sha256(code.encode()+shapely.to_wkb(shapely.normalize(geometry))).digest()
        return self.database.execute('SELECT 1 FROM seen WHERE digest=?',(digest,)).fetchone() is not None
    def duplicate(self,geometry,code):
        digest=hashlib.sha256(code.encode()+shapely.to_wkb(shapely.normalize(geometry))).digest()
        cursor=self.database.execute('INSERT OR IGNORE INTO seen VALUES (?)',(digest,))
        return cursor.rowcount==0
    def close(self):self.database.close()

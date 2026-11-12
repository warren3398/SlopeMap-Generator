"""Slope-map style JPEG page, composed with bounded RGB image buffers."""
from pathlib import Path
import importlib.util

import numpy as np
from PIL import Image,ImageDraw,ImageFont
from pyproj import Transformer

from .export_styles import STYLES,RANGES

LAYOUT_VERSION='framed-location-v2'
GREEN='#005b3c'
INK='#202d27'
LINE='#b6c0b9'
PALE='#e8eee9'


def class_areas(canvas,bounds,class_count):
    """Equal-area hectares of classified pixels; vector boundary detail is estimated."""
    rows,columns=canvas.shape
    left,bottom,right,top=bounds
    transform=Transformer.from_crs(3857,6933,always_xy=True)
    y=np.linspace(top,bottom,rows+1)
    _,equal_y=transform.transform(np.full(rows+1,left),y)
    x0,_=transform.transform(left,(bottom+top)/2)
    x1,_=transform.transform(right,(bottom+top)/2)
    row_ha=np.abs(np.diff(equal_y))*abs(x1-x0)/columns/10000
    hectares=np.zeros(class_count+1,dtype='float64')
    for row,area in zip(canvas,row_ha):
        hectares+=np.bincount(row,minlength=class_count+1)[:class_count+1]*area
    return hectares[1:]


def _font(size,bold=False):
    name='DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf'
    paths=[Path('/usr/share/fonts/truetype/dejavu')/name]
    spec=importlib.util.find_spec('matplotlib')
    if spec and spec.origin:
        paths.append(Path(spec.origin).parent/'mpl-data/fonts/ttf'/name)
    for path in paths:
        if path.exists():return ImageFont.truetype(str(path),size)
    return ImageFont.load_default(size=size)


def compose(canvas,bounds,layer,title,target,boundary_ha=None):
    styles=STYLES[layer]
    areas=class_areas(canvas,bounds,len(styles))
    total=float(areas.sum())
    coverage=100*total/boundary_ha if boundary_ha else None
    if coverage is not None and coverage<50:
        raise ValueError(f'Existing slope source covers only {coverage:.1f}% of the selected area. Complete slope source data is needed for this export.')
    page=Image.new('RGB',(3000,1880),'white')
    draw=ImageDraw.Draw(page)
    draw.rectangle((34,26,2966,1852),outline='#59645d',width=3)
    draw.rectangle((104,180,2004,1720),outline=LINE,width=3)
    draw.text((1054,97),f'{layer.upper()} CLASSIFICATION MAP',font=_font(51,True),fill=INK,anchor='mm')

    palette=np.array([(255,255,255,0)]+[tuple(rgb)+(255,) for _,_,rgb in styles],dtype='uint8')
    image=Image.fromarray(palette[canvas])
    scale=min(1750/image.width,1370/image.height)
    image=image.resize((max(1,round(image.width*scale)),max(1,round(image.height*scale))),Image.Resampling.NEAREST)
    page.paste(image,(104+(1900-image.width)//2,180+(1540-image.height)//2),image)
    del image
    draw.rectangle((1832,248,1936,388),fill='white',outline='#79847d',width=3)
    draw.text((1884,287),'N',font=_font(27,True),fill=INK,anchor='mm')
    draw.polygon([(1884,314),(1872,343),(1896,343)],fill=INK)

    x0,x1=2092,2884
    mid=(x0+x1)//2
    def band(y,height,text):
        draw.rectangle((x0,y,x1,y+height),fill=GREEN)
        draw.text((mid,y+height//2),text,font=_font(31,True),fill='white',anchor='mm')
    band(152,142,layer.upper()+' MAP')
    lines=[line.strip().upper() for line in title.splitlines() if line.strip()]
    spacing=30
    first_y=355-(len(lines)-1)*spacing/2
    for index,label in enumerate(lines):
        size=40 if len(lines)==1 else 30 if len(lines)==2 else 26
        while draw.textlength(label,font=_font(size,True))>750 and size>10:size-=1
        draw.text((mid,first_y+index*spacing),label,font=_font(size,True),fill=INK,anchor='mm')
    band(404,72,layer.upper()+' CLASS LEGEND')
    y=500
    for code,description,rgb in styles:
        draw.rectangle((2120,y+4,2168,y+40),fill=tuple(rgb),outline='#a4afa7')
        draw.text((2188,y+22),code,font=_font(22,True),fill=INK,anchor='lm')
        size=22
        while draw.textlength(description,font=_font(size))>550 and size>16:size-=1
        if layer=='slope':
            draw.text((2230,y+22),RANGES[code],font=_font(25),fill=INK,anchor='lm')
            draw.text((2472,y+22),description,font=_font(23),fill=INK,anchor='lm')
        else:
            draw.text((2322,y+22),description,font=_font(size),fill=INK,anchor='lm')
        y+=72 if layer=='slope' else 44

    summary_y=y+24
    band(summary_y,72,layer.upper()+' CLASS SUMMARY')
    table_y=summary_y+96
    columns=[2092,2252,2582,2884]
    centers=[(a+b)//2 for a,b in zip(columns,columns[1:])]
    def row(y,height,values,fill='white',bold=False):
        draw.rectangle((x0,y,x1,y+height),fill=fill,outline='#d8e0da',width=2)
        for x in columns[1:-1]:draw.line((x,y,x,y+height),fill='#d8e0da',width=2)
        for x,value in zip(centers,values):draw.text((x,y+height//2),str(value),font=_font(23 if bold else 22,bold),fill=INK,anchor='mm')
    row(table_y,54,['CLASS','AREA (HA)','AREA (%)'],PALE,True)
    table_y+=54
    for (code,_,_),area in zip(styles,areas):
        height=58 if layer=='slope' else 36
        row(table_y,height,[code,f'{area:,.2f}',f'{area/total*100 if total else 0:.2f}'])
        table_y+=height
    row(table_y,54,['TOTAL',f'{total:,.2f}','100.00' if total else '0.00'],PALE,True)
    info_y=1568 if layer=='slope' else table_y+78
    band(info_y,80,'MAP INFORMATION')
    draw.text((2108,info_y+99),'CRS: EPSG:3857 | Source: Slope GeoPackage',font=_font(19),fill=INK,anchor='lm')
    draw.text((2108,info_y+127),f'Classified area: {total:,.2f} ha (raster estimate; equal-area)',font=_font(18),fill=INK,anchor='lm')
    if coverage is not None:
        draw.text((2108,info_y+155),f'Source coverage: {min(coverage,100):.1f}% of selected boundary',font=_font(18),fill=INK,anchor='lm')
    page.save(target,format='JPEG',quality=94,dpi=(180,180))
    return {'areas_ha':areas.tolist(),'total_ha':total,'area_method':'equal-area classified raster estimate'}

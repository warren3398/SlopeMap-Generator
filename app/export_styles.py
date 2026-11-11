"""Slope classification definitions, shared by JPEG and vector exports."""
RANGES={'A':'0–3%','B':'3–8%','C':'8–18%','D':'18–30%','E':'30–50%','F':'>50%'}
STYLES={'slope':[
 ('A','Level to nearly level',(64,139,72)),
 ('B','Gently sloping',(136,179,63)),
 ('C','Sloping',(215,233,64)),
 ('D','Moderately steep',(255,227,72)),
 ('E','Steep',(255,161,61)),
 ('F','Very steep',(255,8,8)),
]}

def classify(value):
    value=str(value).strip().upper()
    if value in 'ABCDEF' and len(value)==1:return value
    numeric={str(i+1):code for i,code in enumerate('ABCDEF')}
    if value in numeric:return numeric[value]
    normalized=value.replace('%','').replace(' ','').replace('–','-')
    return {'0-3':'A','3-8':'B','8-18':'C','18-30':'D','30-50':'E','>50':'F'}.get(normalized,'')

def find_field(frame,layer='slope'):
    lookup={str(c).upper():c for c in frame.columns}
    for key in ('SLOPE_CLASS','SLOPE_CLS','SLOPECLASS','SLOPE_RANGE','SLOPE_CAT','GRIDCODE','CLASS','SLOPE'):
        if key in lookup:return lookup[key]
    raise ValueError('Source slope classification field was not found.')

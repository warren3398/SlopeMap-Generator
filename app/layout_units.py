"""Draw the existing layout directly at 4K with native-size text and strokes."""
from PIL import ImageDraw

SCALE=3840/3000
PAGE_SIZE=(3840,round(1880*SCALE))


def px(value):return round(value*SCALE)


def coordinates(values):
    if isinstance(values[0],(tuple,list)):
        return [tuple(value*SCALE for value in point) for point in values]
    return tuple(value*SCALE for value in values)


class LayoutDraw:
    def __init__(self,image):self.draw=ImageDraw.Draw(image)
    def rectangle(self,xy,**kwargs):
        if 'width' in kwargs:kwargs['width']=max(1,px(kwargs['width']))
        return self.draw.rectangle(coordinates(xy),**kwargs)
    def line(self,xy,**kwargs):
        if 'width' in kwargs:kwargs['width']=max(1,px(kwargs['width']))
        return self.draw.line(coordinates(xy),**kwargs)
    def polygon(self,xy,**kwargs):return self.draw.polygon(coordinates(xy),**kwargs)
    def text(self,xy,text,**kwargs):return self.draw.text(coordinates(xy),text,**kwargs)
    def textlength(self,text,**kwargs):return self.draw.textlength(text,**kwargs)/SCALE

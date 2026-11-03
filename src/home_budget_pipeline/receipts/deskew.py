"""Conservative small-angle deskew from agreement across printed page regions."""
from math import cos, radians, sin
from statistics import median

POLICY = 'regional-projection-deskew-v1'
MAX_ANGLE = 12


def _score(mask, angle):
    from PIL import Image
    rotated = mask.rotate(angle, resample=Image.Resampling.NEAREST,
                          expand=True, fillcolor=0)
    # Native BOX projection avoids walking full-resolution pixels in Python.
    projection = rotated.resize((1, rotated.height), Image.Resampling.BOX)
    rows = list(projection.get_flattened_data())
    return sum((b-a)**2 for a,b in zip(rows,rows[1:])) * rotated.width**2


def estimate(image):
    from PIL import Image, ImageOps
    gray = ImageOps.autocontrast(image.convert('L'))
    gray.thumbnail((1000, 1800))
    mask = gray.point(lambda p:255 if p<160 else 0)
    bbox = mask.getbbox()
    result = {'policy':POLICY, 'angle_degrees':0.0, 'status':'unchanged',
              'reason':'insufficient_consistent_text', 'regions':[]}
    if not bbox:
        return result
    mask = mask.crop(bbox)
    if min(mask.size)<40:
        return result
    angles=[]
    for part in range(3):
        region=mask.crop((0,part*mask.height//3,mask.width,(part+1)*mask.height//3))
        coarse=[(angle,_score(region,angle)) for angle in range(-MAX_ANGLE,MAX_ANGLE+1)]
        best=max(coarse,key=lambda item:item[1])[0]
        fine=[(best+i/10,_score(region,best+i/10)) for i in range(-9,10)
              if abs(best+i/10)<MAX_ANGLE]
        angle,score=max(fine,key=lambda item:item[1])
        baseline=_score(region,0)
        gain=score/baseline if baseline else 0
        result['regions'].append({'angle_degrees':round(angle,2),'gain':round(gain,4)})
        if gain>=1.15 and .2<=abs(angle)<MAX_ANGLE-.5:
            angles.append(angle)
    # Isolated rules/logos and mixed orientations must not decide rotation.
    agreed=[a for a in angles if sum(abs(a-b)<=.4 for b in angles)>=2]
    if len(agreed)>=2 and max(agreed)-min(agreed)<=.4:
        result.update(angle_degrees=round(median(agreed),2),status='rotated',
                      reason='consistent_regional_projection')
    return result


def apply(image, decision=None):
    """Expand the canvas, preserving edges; record source-to-canvas coordinates."""
    from PIL import Image
    decision = estimate(image) if decision is None else decision
    angle=decision['angle_degrees']
    original=image.size
    if angle:
        fill=255 if image.mode=='L' else (255,255,255)
        image=image.convert('L' if image.mode=='L' else 'RGB').rotate(
            angle,resample=Image.Resampling.BICUBIC,expand=True,fillcolor=fill)
    c,s=cos(radians(angle)),sin(radians(angle))
    metadata={**decision,'input_size':list(original),'output_size':list(image.size),
              'source_to_deskew_canvas':[c,s,image.width/2-c*original[0]/2-s*original[1]/2,
                                       -s,c,image.height/2+s*original[0]/2-c*original[1]/2]}
    return image,metadata

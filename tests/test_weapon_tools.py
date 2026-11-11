import json
from pathlib import Path
import re
import tempfile
import unittest
from PIL import Image
from mcp_server import weapon_tools as w


def manifest():
    colors = {v:k for k,v in w.ANGLES.items()}
    return dict(width=16,height=16,tags=[],frames=[dict(duration=100,markers=[[4,7,*colors[a],255]]) for a in w.EXPECTED])


class WeaponTests(unittest.TestCase):
    def test_rotation_centers_alpha_and_no_gaps(self):
        im = Image.new('RGBA',(5,9),(20,40,60,255))
        im.putpixel((2,7),(200,20,30,255))
        for angle in (0,15,-45,90,135,-25):
            out = w._place(im,(32,32),(2,7),(12,15),angle)
            self.assertEqual(out.getpixel((12,15)), im.getpixel((2,7)))
            self.assertLessEqual(set(out.getchannel('A').getdata()),{0,255})
            for y in range(32):
                xs=[x for x in range(32) if out.getpixel((x,y))[3]]
                if xs: self.assertEqual(xs,list(range(min(xs),max(xs)+1)))
        out=w._place(im,(32,32),(2,7),(12,15),90)
        self.assertEqual(out.getpixel((19,15)),im.getpixel((2,0)))
        self.assertEqual(out.getpixel((12,8))[3],0)

    def test_sockets(self):
        data=manifest()
        result=w._sockets(data)
        self.assertEqual(result[0],dict(frame=1,grip_x=4,grip_y=7,angle_deg=15))
        for point in ((-3,-7),(16,7),(4,16),(4.0,7),(True,7)):
            bad=manifest(); bad['frames'][0]['markers'][0][:2]=point
            with self.assertRaisesRegex(ValueError,'coordinates'): w._sockets(bad)
        self.assertEqual([p['angle_deg'] for p in result],w.EXPECTED)
        for markers in ([],[[0,0,255,0,0,255]]*2,[[0,0,254,0,0,255]],[[0,0,255,0,0,128]],[[0,0,0,255,0,255]]):
            data=manifest(); data['frames'][0]['markers']=markers
            with self.assertRaises(ValueError): w._sockets(data)
        with self.assertRaises(ValueError): w._sockets(dict(frames=[]))

    def test_ids(self):
        for value in ('','../x','a\\b','CON','NUL.txt','a\n','a\x00','LPT9','a.','a ','x:y'):
            with self.assertRaises(ValueError,msg=repr(value)): w._safe_id(value)
        self.assertEqual(w._safe_id('剑_01'),'剑_01')

    def test_inspect_and_pivots(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'weapon.png'
            im=Image.new('RGBA',(7,20)); im.putpixel((1,4),(1,2,3,128)); im.putpixel((3,18),(1,2,3,127)); im.save(p)
            self.assertEqual(w._pivot(w._weapon(p)),(1,4))
            self.assertIn('suggested pivot: (1, 4)',w.inspect_weapon_ascii(p))
            for x,y in ((-1,4),(7,4),(1.0,4),(True,4),(3,18)):
                with self.assertRaises(ValueError):
                    w.equip_and_export_weapon(p,p,Path(d),'sword',x,y,run_lua=None,run_aseprite=None,lua_string=json.dumps)
            Image.new('RGBA',(2,2)).save(p)
            with self.assertRaises(ValueError): w.inspect_weapon_ascii(p)

    def test_bbox_pivot_padding_and_empty_row(self):
        im=Image.new("RGBA",(12,24))
        for y in range(3,23):
            if y != 20:
                for x in (2,3,7): im.putpixel((x,y),(10,20,30,255))
        # bbox bottom 23 - max(2, int(20*.18)) = 20; nearest row 19.
        self.assertEqual(w._pivot(im),(3,19))
        padded=Image.new("RGBA",(40,60)); padded.paste(im,(8,9))
        self.assertEqual(w._pivot(padded),(11,28))
        # Horizontal mean can be transparent, so choose the nearest opaque pixel.
        sparse=Image.new("RGBA",(10,10))
        sparse.putpixel((1,7),(1,2,3,255)); sparse.putpixel((7,7),(1,2,3,255))
        self.assertEqual(w._pivot(sparse),(1,7))

    def test_mock_export_and_failures(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); weapon=root/'input.png'; source=root/'input.aseprite'
            Image.new('RGBA',(3,8),(60,70,80,255)).save(weapon)
            source.write_bytes(b'unchanged')
            scripts=[]; calls=[]; state={'fail':None}
            def lua(script):
                scripts.append(script)
                if 'manifest.json' in script:
                    dest=Path(json.loads(re.search(r'io.open\((".*?"),"w"',script)[1]))
                    dest.write_text(json.dumps(manifest()))
                else:
                    dest=Path(json.loads(re.search(r's:saveAs\((".*?")\)',script)[1]))
                    dest.write_bytes(b'native mock')
            def run(args):
                calls.append(args)
                if state['fail']=='raise': raise RuntimeError('export failed')
                if state['fail']=='missing': return ''
                if '--sheet' in args:
                    sheet=Path(args[args.index('--sheet')+1]); meta=Path(args[args.index('--data')+1])
                    im=Image.new('RGBA',(256,16))
                    for i in range(16):
                        with Image.open(sheet.parent/f'pose_{i+1}.png') as pose: im.paste(pose,(i*16,0))
                    im.save(sheet)
                    frames=[dict(duration=100,trimmed=False,frame=dict(x=i*16,y=0,w=16,h=16)) for i in range(16)]
                    if state['fail']=='json': frames.pop()
                    meta.write_text(json.dumps(dict(frames=frames,meta=dict(frameTags=[]))))
                else: Image.new('RGBA',(64,64)).save(args[-1],format='GIF')
            def equip():
                return w.equip_and_export_weapon(source,weapon,root/'out','剑',1,7,run_lua=lua,run_aseprite=run,lua_string=lambda s:json.dumps(s,ensure_ascii=False))
            result=equip()
            self.assertTrue(all(Path(result[k]).is_absolute() for k in ('equipped_ase','sheet','json','gif','sockets')))
            self.assertEqual(source.read_bytes(),b'unchanged')
            self.assertEqual(calls[0][:2],['--layer','Weapon'])
            self.assertIn('it.x+cel.position.x',scripts[0])
            self.assertIn('it.y+cel.position.y',scripts[0])
            self.assertNotIn('isVisible=true',scripts[0])
            self.assertIn('s:deleteLayer(found[1])',scripts[1])
            self.assertIn('parent=found[1].parent index=found[1].stackIndex',scripts[1])
            self.assertIn('layer.parent=parent layer.stackIndex=index',scripts[1])
            self.assertIn('assert(not found[1].isGroup',scripts[1])
            self.assertIn('pivot_x=1, pivot_y=6',w.inspect_weapon_ascii(weapon))
            for failure in ('raise','missing','json'):
                state['fail']=failure
                with self.assertRaises(RuntimeError): equip()
            sockets=Path(result['sockets']); sockets.write_text('[]')
            with self.assertRaises(ValueError): equip()


if __name__ == '__main__':
    unittest.main()

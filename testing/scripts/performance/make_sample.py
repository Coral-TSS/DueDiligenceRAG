"""用 PDF 环境的 Pillow 生成公开合成样本；不读取客户文件。"""
import io
from pathlib import Path
import sys


def make_sample(target):
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new('RGB', (900, 260), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype('C:/Windows/Fonts/simsun.ttc', 42)
    draw.text((45, 65), '注册资本2000万元', font=font, fill='black')
    buffer = io.BytesIO(); image.save(buffer, format='JPEG', quality=95)
    jpeg = buffer.getvalue()
    native = b'BT /F1 14 Tf 50 200 Td (Company registered capital is 2000. Established in 2014.) Tj ET'
    scan = b'q 595 0 0 172 0 0 cm /Im0 Do Q'
    def stream(value):
        return b'<< /Length '+str(len(value)).encode()+b' >>\nstream\n'+value+b'\nendstream'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Count 2 /Kids [3 0 R 5 0 R] >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 260] /Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> /Contents 4 0 R >>',
        stream(native),
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 172] /Resources << /XObject << /Im0 7 0 R >> >> /Contents 6 0 R >>',
        stream(scan),
        b'<< /Type /XObject /Subtype /Image /Width 900 /Height 260 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length '+str(len(jpeg)).encode()+b' >>\nstream\n'+jpeg+b'\nendstream']
    data = b'%PDF-1.4\n'; offsets = [0]
    for n, obj in enumerate(objects, 1):
        offsets.append(len(data)); data += f'{n} 0 obj\n'.encode()+obj+b'\nendobj\n'
    xref = len(data)
    data += f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode()
    data += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
    data += f'trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF'.encode()
    Path(target).write_bytes(data)


if __name__ == '__main__':
    make_sample(sys.argv[1])

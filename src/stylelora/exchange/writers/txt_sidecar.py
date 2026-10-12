import os
import shutil
from stylelora.project_db.errors import UnsupportedScheme
from ..files import sha256
from ..types import FileResult


class TxtSidecarWriter:
    name = 'image_txt'

    def write_caption(self,path,caption,*,encoding='utf-8'):
        if encoding != 'utf-8':
            raise UnsupportedScheme('encoding:unsupported')
        with path.open('xb') as stream:
            stream.write(caption.encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        return FileResult(path.name,path.stat().st_size,sha256(path),'caption')

    def write_image(self,src,dst,*,mode='copy'):
        if mode != 'copy':
            raise UnsupportedScheme('image_mode:unsupported')
        with src.open('rb') as source, dst.open('xb') as target:
            shutil.copyfileobj(source,target)
            target.flush()
            os.fsync(target.fileno())
        return FileResult(dst.name,dst.stat().st_size,sha256(dst),'copy')

"""Select explicitly authorized reference files through either browser picker.

The page receives only File objects for the supplied files, never filesystem
paths, directory handles, writable handles, or a general filesystem binding.
"""
from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from weakref import WeakKeyDictionary


class ReferencePickerError(ValueError):
    pass


_PAGE_LOCKS = WeakKeyDictionary()
_MIME = {'.png':'image/png', '.jpg':'image/jpeg', '.jpeg':'image/jpeg',
         '.webp':'image/webp', '.mp4':'video/mp4', '.mov':'video/quicktime'}
_MAX_FILE = 50 * 1024 * 1024
_MAX_TOTAL = 70 * 1024 * 1024

_INSTALL = r'''items => {
    const descriptor=Object.getOwnPropertyDescriptor(window,'showOpenFilePicker');
    const original=window.showOpenFilePicker;
    const state={selected:false,invocations:0,error:null,installed:false,closed:false};
    let selectedFiles=[];
    let replacement;
    state.restore=()=>{
        state.closed=true;
        selectedFiles=[];
        if(state.installed&&window.showOpenFilePicker===replacement){
            if(descriptor)Object.defineProperty(window,'showOpenFilePicker',descriptor);
            else delete window.showOpenFilePicker;
        }
    };
    // Preserve the site's feature detection in browsers without this API.
    if(typeof original!=='function')return state;
    selectedFiles=items.map(item=>{
        const binary=atob(item.base64),bytes=new Uint8Array(binary.length);
        for(let index=0;index<binary.length;index++)bytes[index]=binary.charCodeAt(index);
        return new File([bytes],item.name,{type:item.mime,lastModified:0});
    });
    replacement=async function(options={}){
        if(state.closed||state.invocations++)throw new DOMException('This reference selection was already used.','AbortError');
        if(selectedFiles.length>1&&options?.multiple!==true){
            state.error='multiple_not_allowed';
            throw new DOMException('This reference picker accepts only one file.','AbortError');
        }
        const handles=selectedFiles.map(file=>Object.freeze({kind:'file',name:file.name,getFile:async()=>file}));
        state.selected=true;
        return handles;
    };
    Object.defineProperty(window,'showOpenFilePicker',{
        configurable:true,enumerable:descriptor?.enumerable??true,writable:true,value:replacement
    });
    state.installed=true;
    return state;
}'''


def _files(files):
    if not isinstance(files,(list,tuple)) or not 1<=len(files)<=50:
        raise ReferencePickerError('Select between 1 and 50 reference files.')
    payloads=[];total=0
    for source in files:
        try:
            path=Path(source)
            mime=_MIME.get(path.suffix.lower())
            if not mime:raise ReferencePickerError('Unsupported reference file format.')
            size=path.stat().st_size
            if not 0<size<=_MAX_FILE or total+size>_MAX_TOTAL:
                raise ReferencePickerError('Reference files exceed the allowed upload size.')
            data=path.read_bytes()
            if len(data)!=size:raise ReferencePickerError('A reference file changed while being read.')
            total+=len(data)
            payloads.append({'name':path.name,'mimeType':mime,'buffer':data})
        except ReferencePickerError:raise
        except (OSError,TypeError,ValueError):
            raise ReferencePickerError('A supplied reference file could not be read.') from None
    return payloads


async def choose_reference_files(page,button,files,timeout_ms=20000):
    """Click once and return the selected mechanism, restoring picker state always."""
    if type(timeout_ms) is not int or not 1<=timeout_ms<=120000:
        raise ReferencePickerError('Reference picker timeout must be 1–120000 milliseconds.')
    lock=_PAGE_LOCKS.setdefault(page,asyncio.Lock())
    async with lock:
        payloads=_files(files)
        items=[{'name':p['name'],'mime':p['mimeType'],'base64':base64.b64encode(p['buffer']).decode('ascii')} for p in payloads]
        chooser=asyncio.get_running_loop().create_future()
        state=None
        def on_chooser(value):
            if not chooser.done():chooser.set_result(value)
        page.on('filechooser',on_chooser)
        try:
            async with asyncio.timeout(timeout_ms/1000):
                state=await page.evaluate_handle(_INSTALL,items)
                await button.click(timeout=timeout_ms)
                while True:
                    observed=await state.evaluate('s=>({selected:s.selected,error:s.error})')
                    if observed['selected']:
                        return {'mechanism':'file-system-access','count':len(payloads)}
                    if chooser.done():
                        selected=chooser.result()
                        if len(payloads)>1 and not selected.is_multiple():
                            raise ReferencePickerError('This reference picker accepts only one file.')
                        await selected.set_files(payloads,timeout=timeout_ms)
                        return {'mechanism':'html-filechooser','count':len(payloads)}
                    if observed['error']=='multiple_not_allowed':
                        raise ReferencePickerError('This reference picker accepts only one file.')
                    await asyncio.sleep(.05)
        except ReferencePickerError:raise
        except TimeoutError:
            raise ReferencePickerError('Reference file selection timed out; the button was not clicked again.') from None
        except Exception:
            raise ReferencePickerError('The page could not accept the supplied reference files.') from None
        finally:
            page.remove_listener('filechooser',on_chooser)
            if not chooser.done():chooser.cancel()
            if state is not None:
                try:await state.evaluate('s=>s.restore()')
                except Exception:pass  # Navigation/closed context also discards the temporary wrapper.
                finally:
                    try:await state.dispose()
                    except Exception:pass
            items.clear();payloads.clear()

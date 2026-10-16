import functools
import threading
from pathlib import Path

_openclip_patch_lock = threading.Lock()


def resolve_cached_hf_model(model_path: str) -> str:
    """Return the local snapshot directory of a Hugging Face model that is in the local cache, else model_path.

    The DAM loader calls the Hugging Face API (repo_exists) for a repository id even when its weights are cached,
    which raises with HF_HUB_OFFLINE=1 and without network access. A local directory needs no network at all. A model
    that is not cached is left alone, so it is downloaded as before; delete the cached copy to update it.
    """
    if "/" not in model_path or Path(model_path).exists():
        return model_path
    try:
        from huggingface_hub import snapshot_download

        return snapshot_download(model_path, local_files_only=True)
    except Exception:
        return model_path


def create_openclip_model(model_name: str, pretrained: str, device: str):
    """open_clip.create_model_and_transforms() that takes the weights from the local Hugging Face cache when they are there.

    open_clip asks the Hub for the file's metadata even when the file is cached, and huggingface_hub raises on an SSL or
    proxy error in that request instead of using the cached copy. One dropped connection to the proxy then killed the
    grounding worker, and the run finished without a single object description. Weights that are not cached are
    downloaded as before; delete the cached copy to update it.
    """
    import open_clip
    import open_clip.pretrained as openclip_pretrained

    def create():
        return open_clip.create_model_and_transforms(model_name, pretrained=pretrained, device=device)

    # open_clip.pretrained.hf_hub_download is the only place where the Hub is contacted; the lock keeps two threads
    # from saving each other's patched function as the original
    with _openclip_patch_lock:
        hub_download = openclip_pretrained.hf_hub_download
        openclip_pretrained.hf_hub_download = functools.partial(hub_download, local_files_only=True)
        try:
            return create()
        except Exception:
            pass  # not in the cache (or incomplete): download as usual below
        finally:
            openclip_pretrained.hf_hub_download = hub_download
    return create()

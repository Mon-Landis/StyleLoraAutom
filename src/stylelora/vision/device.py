from dataclasses import dataclass
import subprocess


@dataclass(frozen=True)
class DeviceInfo:
    provider: str
    total_bytes: int | None = None
    free_bytes: int | None = None
    reason: str | None = None


def probe_device(*, nvml=None, smi=None, providers=None):
    try:
        if nvml is None:
            import pynvml as nvml
        nvml.nvmlInit()
        try:
            memory = nvml.nvmlDeviceGetMemoryInfo(nvml.nvmlDeviceGetHandleByIndex(0))
            total, free = memory.total, memory.free
        finally:
            nvml.nvmlShutdown()
    except Exception:
        try:
            output = (smi or subprocess.check_output)([
                'nvidia-smi', '--query-gpu=memory.total,memory.free', '--format=csv,noheader,nounits'],
                timeout=3, text=True)
            total, free = (int(v.strip())*1024**2 for v in output.splitlines()[0].split(','))
        except Exception:
            return DeviceInfo('cpu', reason='memory_probe_unavailable')
    if providers is None:
        try:
            import onnxruntime
            providers = onnxruntime.get_available_providers()
        except ImportError:
            providers = ()
    if 'CUDAExecutionProvider' not in providers:
        return DeviceInfo('cpu', total, free, 'cuda_unavailable')
    return DeviceInfo('cuda', total, free)


def plan_batch(device, manifest, requested):
    if type(requested) is not int or requested < 1:
        raise ValueError('batch_invalid')
    profile = manifest.tensor_schema.get('memory_per_image')
    if device.provider == 'cpu' or not profile or not device.free_bytes or not device.total_bytes:
        return 1
    usable = device.free_bytes - max(1024**3, device.total_bytes//5)
    return max(1, min(requested, 4, int(usable//profile)))

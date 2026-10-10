"""Відеокарта: програма сама бачить, що в комп'ютері, бере CUDA на NVIDIA, а процесор — завжди запасний."""

import types

import pytest

from autopara import accel, ears
from autopara.accel import CPU, Device, Gpu, _linux_gpus, _vendor_of


def card(root, name, vendor, driver, vram=None):
    device = root / name / "device"
    device.mkdir(parents=True)
    (device / "vendor").write_text(vendor + "\n")
    (device / "uevent").write_text(f"DRIVER={driver}\nPCI_CLASS=30000\n")
    if vram is not None:
        (device / "mem_info_vram_total").write_text(f"{vram}\n")


def test_linux_cards_are_read_from_sysfs(tmp_path):
    card(tmp_path, "card0", "0x8086", "i915")
    card(tmp_path, "card1", "0x10de", "nvidia")
    card(tmp_path, "card2", "0x1002", "amdgpu", vram=512 * 2**20)  # APU: вбудована
    card(tmp_path, "card3", "0x1002", "amdgpu", vram=8 * 2**30)
    (tmp_path / "card1-HDMI-A-1" / "device").mkdir(parents=True)  # роз'єм, а не карта
    found = _linux_gpus(tmp_path)
    assert [(g.vendor, g.integrated) for g in found] == [("intel", True), ("nvidia", False), ("amd", True), ("amd", False)]
    assert _vendor_of("NVIDIA GeForce RTX 3050 Laptop GPU") == "nvidia"
    assert _vendor_of("AMD Radeon(TM) Graphics") == "amd" and _vendor_of("Intel(R) Iris(R) Xe Graphics") == "intel"


@pytest.fixture
def machine(monkeypatch):
    """Комп'ютер з потрібними картами й відповіддю nvidia-smi."""
    def setup(cards, cuda_devices=0, memory=None, smi=True):
        monkeypatch.setattr(accel, "gpus", lambda: tuple(cards))
        monkeypatch.setattr(accel.shutil, "which", lambda name: "/usr/bin/nvidia-smi" if smi and name == "nvidia-smi" else None)
        monkeypatch.setattr(accel, "nvidia_memory", lambda: memory)
        monkeypatch.setattr(accel, "load_cuda_libraries", lambda: None)
        monkeypatch.setitem(__import__("sys").modules, "ctranslate2",
                            types.SimpleNamespace(get_cuda_device_count=lambda: cuda_devices))
    return setup


def test_nvidia_gets_cuda_sized_to_its_memory(machine):
    machine([Gpu("intel", "Iris Xe", True), Gpu("nvidia", "RTX 4060")], cuda_devices=1, memory=("RTX 4060", 8.0, 7.5))
    device = accel.whisper_device()
    # 8 ГБ: стиснутий large-v3 (~2 ГБ) — поруч ще влазить мовна модель; повна точність — від 10 ГБ.
    assert device == Device("cuda", "int8_float16", "RTX 4060 (CUDA)", 8.0)
    assert accel.live_model(device, 8, 16) == "large-v3"  # на карті найточніша встигає й наживо
    machine([Gpu("nvidia", "RTX 4070 Ti")], cuda_devices=1, memory=("RTX 4070 Ti", 12.0, 11.0))
    assert accel.whisper_device().compute_type == "float16"
    machine([Gpu("nvidia", "MX450")], cuda_devices=1, memory=("MX450", 2.0, 1.9))
    small = accel.whisper_device()
    assert small.compute_type == "int8_float16" and accel.live_model(small, 8, 16) == "large-v3-turbo"
    # Уточнення після пари рахує вільну пам'ять: вуха наживо вже зайняли карту — тоді процесор.
    machine([Gpu("nvidia", "RTX 3050")], cuda_devices=1, memory=("RTX 3050", 4.0, 1.0))
    assert accel.whisper_device(free=True) == CPU and accel.whisper_device().gpu


def test_no_usable_cuda_means_processor(machine):
    machine([Gpu("intel", "Iris Xe", True)], smi=False)
    assert accel.whisper_device() == CPU and accel.live_model(CPU, 8, 16) == "large-v3-turbo"
    machine([Gpu("nvidia", "GTX 1050")], cuda_devices=0)  # драйвер не той — CUDA не бачить карту
    assert accel.whisper_device() == CPU
    machine([Gpu("nvidia", "RTX 4060")], cuda_devices=1, memory=("RTX 4060", 8.0, 8.0))
    assert accel.whisper_device("cpu") == CPU  # людина сама сказала — процесор


def test_language_model_stays_off_integrated_graphics(machine, monkeypatch):
    monkeypatch.setattr(accel.sys, "platform", "linux")
    machine([Gpu("intel", "Iris Xe", True)], smi=False)
    assert not accel.brain_on_gpu()  # Vulkan на вбудованій вішає карту (GPU HANG) — лише процесор
    machine([Gpu("intel", "Iris Xe", True), Gpu("nvidia", "RTX 3050")])
    assert accel.brain_on_gpu() and accel.pip_extras() == "ears,cuda"
    machine([Gpu("intel", "Iris Xe", True)], smi=False)
    assert accel.pip_extras() == "ears"


def test_transcriber_falls_back_to_processor_when_cuda_breaks(machine, monkeypatch):
    machine([Gpu("nvidia", "RTX 3050")], cuda_devices=1, memory=("RTX 3050", 4.0, 4.0))
    monkeypatch.setattr(ears.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(ears, "total_ram_gb", lambda: 16)
    loaded = []

    class WhisperModel:
        def __init__(self, name, device, compute_type):
            loaded.append((name, device, compute_type))
            self.device = device

        def transcribe(self, audio, **options):
            if self.device == "cuda":
                raise RuntimeError("Library libcudnn_ops.so.9 is not found")
            return iter([]), None

    module = types.ModuleType("faster_whisper")
    module.WhisperModel = WhisperModel
    monkeypatch.setitem(__import__("sys").modules, "faster_whisper", module)
    transcriber = ears.WhisperTranscriber("auto", "uk")
    # На карті — large-v3; карта не впоралась — процесор і модель, що встигає на ньому.
    assert loaded == [("large-v3", "cuda", "int8_float16"), ("large-v3-turbo", "cpu", "int8")]
    assert transcriber.device == CPU and transcriber.name == "large-v3-turbo"
    loaded.clear()
    ears.WhisperTranscriber("large-v3", "uk")  # явно вибрана модель лишається тією ж і на процесорі
    assert loaded[-1] == ("large-v3", "cpu", "int8")


def test_gpu_out_of_memory_mid_lesson_moves_ears_to_processor(machine, monkeypatch):
    """Карта на 8 ГБ, частину зайняла Ollama — посеред пари Whisper отримує «out of memory». Не мовчки
    помилятися на кожному шматку, а перейти на процесор і розпізнавати далі."""
    machine([Gpu("nvidia", "RTX 4060")], cuda_devices=1, memory=("RTX 4060", 8.0, 8.0))
    monkeypatch.setattr(ears.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(ears, "total_ram_gb", lambda: 16)
    loaded, full = [], [False]

    class Segment:
        def __init__(self, text):
            self.text, self.start, self.end, self.avg_logprob, self.no_speech_prob = text, 0.0, 2.0, -0.2, 0.01

    class WhisperModel:
        def __init__(self, name, device, compute_type):
            loaded.append((name, device))
            self.device = device

        def transcribe(self, audio, **options):
            if self.device == "cuda" and full[0]:
                raise RuntimeError("CUDA failed with error out of memory")
            return iter([Segment("Добрий день")]), None

    module = types.ModuleType("faster_whisper")
    module.WhisperModel = WhisperModel
    monkeypatch.setitem(__import__("sys").modules, "faster_whisper", module)
    transcriber = ears.WhisperTranscriber("auto", "uk")
    assert transcriber.device.gpu and loaded[-1] == ("large-v3", "cuda")
    full[0] = True  # Ollama зайняла карту
    assert [h.text for h in transcriber(b"\x00\x00" * 16000)] == ["Добрий день"]
    assert transcriber.device == CPU and loaded[-1] == ("large-v3-turbo", "cpu")

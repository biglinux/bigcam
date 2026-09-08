"""Pure media/transport contracts: no display, devices, network or credentials."""
import io
import math
import random
from types import SimpleNamespace
from PIL import Image
import numpy as np
import pytest

from constants import BackendType
from core.camera_identity import unique_cameras
from core.phone_protocol import StreamAssembler, jpeg_size, valid_token, MAX_FRAME_BYTES
from core.recording_config import RecordingConfig
from utils.frame_buffers import bgr_from_bgra, LatestValue
from utils.video_formats import frame_rate, source_caps


@pytest.mark.parametrize("fps,expected", [(29.970,"30000/1001"),(59.940,"60000/1001"),(23.976,"24000/1001"),(25,"25/1"),(15.5,"31/2")])
def test_fractional_frame_rates(fps, expected):
    assert frame_rate(fps) == expected

@pytest.mark.parametrize("fps", [0,-1,float("nan"),float("inf"),2000])
def test_invalid_rates(fps):
    with pytest.raises(ValueError): frame_rate(fps)

@pytest.mark.parametrize("fourcc,prefix", [("MJPG","image/jpeg"),("H264","video/x-h264"),("HEVC","video/x-h265"),("YUYV","video/x-raw,format=YUY2")])
def test_caps(fourcc,prefix):
    caps, _ = source_caps(SimpleNamespace(pixel_format=fourcc,width=640,height=480,fps=[29.97]))
    assert caps.startswith(prefix) and "30000/1001" in caps

def test_unknown_fourcc_is_not_declared_raw():
    with pytest.raises(ValueError): source_caps(SimpleNamespace(pixel_format="????"))

def test_names_do_not_deduplicate_devices():
    def cam(ident,path,backend=BackendType.V4L2):
        return SimpleNamespace(id=ident,name="Same camera",device_path=path,backend=backend,extra={})
    assert len(unique_cameras([cam("a","/dev/video0"),cam("b","/dev/video2")])) == 2
    result=unique_cameras([cam("pipe","/dev/video0",BackendType.PIPEWIRE),cam("v4l","/dev/video0")])
    assert len(result)==1 and result[0].id=="v4l"

def test_padded_video_is_copied_before_unmap():
    raw = bytearray(48)
    raw[4:12] = bytes([1,2,3,255,4,5,6,255])
    raw[24:32] = bytes([7,8,9,255,10,11,12,255])
    frame = bgr_from_bgra(raw,2,2,20,4)
    assert frame.tolist()==[[[1,2,3],[4,5,6]],[[7,8,9],[10,11,12]]]
    raw[4]=99
    assert frame[0,0,0]==1

@pytest.mark.parametrize("args", [(b"",1,1,4), (b"1234",2,1,8),(b"1234",1,1,3)])
def test_invalid_video_layout(args):
    with pytest.raises(ValueError): bgr_from_bgra(*args)

def test_latest_preview_slot_has_one_callback():
    slot=LatestValue()
    assert slot.publish(0)
    for number in range(1,10000): assert not slot.publish(number)
    assert slot.take()==9999
    assert slot.publish(1)
    slot.clear()
    assert not slot.publish(2)
    assert slot.take()==2
    assert slot.publish(3)

@pytest.mark.parametrize("video",["h264","h265","vp9","mjpeg"])
@pytest.mark.parametrize("audio",["aac","opus","vorbis","mp3"])
def test_webm_compatibility_is_bidirectional(video,audio):
    config=RecordingConfig(video,audio,"webm",7000)
    assert config.video_codec=="vp9" and config.audio_codec in {"opus","vorbis"}
    assert "7000000" in config.encoders()[-1][1]

def test_mp4_valid_codecs_are_preserved():
    assert RecordingConfig("vp9","opus","mp4").video_codec=="vp9"
    assert RecordingConfig("mjpeg","vorbis","mp4").audio_codec=="aac"

@pytest.mark.parametrize("bitrate",[float("nan"),float("inf"),True])
def test_bad_recording_bitrate(bitrate):
    with pytest.raises(ValueError): RecordingConfig(video_bitrate=bitrate)

@pytest.mark.parametrize("value",[None,"","é",b"secret","X"*129])
def test_token_rejects_untrusted_types(value):
    assert not valid_token(value,"secret")

def test_token_exact_match():
    assert valid_token("secret","secret") and not valid_token("Secret","secret")

def test_stream_authentication_and_reset():
    streams=StreamAssembler(4)
    with pytest.raises(PermissionError): streams.feed(8,2,b"a",False)
    assert streams.feed(4,2,b"a",False) is None
    assert streams.feed(4,2,b"bc",True)==b"abc"
    assert streams.total==0 and not streams.streams
    streams.feed(4,6,b"old",False); streams.discard(6)
    assert streams.feed(4,6,b"new",True)==b"new"

def test_stream_limits():
    s=StreamAssembler(4)
    for i in range(8): s.feed(4,i,b"x",False)
    with pytest.raises(ValueError): s.feed(4,9,b"x",False)
    assert len(s.streams)==8
    s=StreamAssembler(4)
    with pytest.raises(ValueError): s.feed(4,1,b"x"*(MAX_FRAME_BYTES+1),False)
    assert s.total==0

def test_expired_stream():
    now=[0.0]; s=StreamAssembler(4,lambda:now[0]);s.feed(4,2,b"a",False)
    now[0]=3.1
    with pytest.raises(ValueError): s.feed(4,2,b"b",True)

def test_jpeg_dimensions_before_decoding():
    stream=io.BytesIO();Image.new("RGB",(82,42)).save(stream,format="JPEG")
    assert jpeg_size(stream.getvalue())==(82,42)
    with pytest.raises(ValueError): jpeg_size(b"not an image")

def test_seeded_fragmentation_fuzz():
    rng=random.Random(73001)
    for _ in range(2000):
        data=rng.randbytes(rng.randrange(0,2048));s=StreamAssembler(4);cut=rng.randrange(len(data)+1)
        assert s.feed(4,2,data[:cut],False) is None
        assert s.feed(4,2,data[cut:],True)==data
        assert not s.streams and s.total==0


def test_certificate_is_private_short_lived_and_reused(tmp_path):
    from core.phone_tls import ensure_certificate
    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric import ec
    from pathlib import Path
    cert,key,digest=ensure_certificate(tmp_path/"tls")
    x=x509.load_pem_x509_certificate(Path(cert).read_bytes())
    assert isinstance(x.public_key(),ec.EllipticCurvePublicKey)
    assert (x.not_valid_after_utc-x.not_valid_before_utc).days<=14
    assert Path(key).stat().st_mode&0o777==0o600
    assert ensure_certificate(tmp_path/"tls")== (cert,key,digest)

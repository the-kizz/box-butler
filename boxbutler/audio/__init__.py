"""Audio layer (Task 14; spec §3.4, §3.5).

`RendererProtocol` is the seam: nothing above this layer knows ffmpeg is
involved. Given a cached source file and a `RenderSpec`, a renderer probes
it, decides a `RenditionMode` (`choose_mode`), and produces a rendition
ready to upload — stream-copy trim where possible, transcode only when the
sink can't accept the source format, and loudness normalisation only when
explicitly asked for (see `boxbutler/audio/ffmpeg.py` for why that default
matters).
"""

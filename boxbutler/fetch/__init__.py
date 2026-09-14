"""Fetch layer (Task 13; spec §2 step 2, §3.5, §3.5.1).

Turns a source reference into a cached audio file behind `FetcherProtocol`.
Nothing above this layer knows whether the audio came from yt-dlp, HTTP, or
a local file.
"""

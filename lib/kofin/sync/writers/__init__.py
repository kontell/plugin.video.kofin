from kofin.sync.writers.movies import Movies
from kofin.sync.writers.musicvideos import MusicVideos
from kofin.sync.writers.tvshows import TVShows
from kofin.sync.writers.music import Music
from kofin.sync.obj import Objects

__all__ = ["Movies", "MusicVideos", "TVShows", "Music"]

# The mapping is package state the writers cannot run without; the fork loads
# it at import time too (objects/__init__.py).
Objects().mapping()

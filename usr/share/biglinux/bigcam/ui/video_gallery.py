"""Video media gallery."""
from ui.media_gallery import MediaGallery
from utils import xdg


class VideoGallery(MediaGallery):
    def __init__(self):
        super().__init__("video", xdg.videos_dir())

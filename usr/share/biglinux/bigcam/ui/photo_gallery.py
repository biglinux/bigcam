"""Photo media gallery."""
from ui.media_gallery import MediaGallery
from utils import xdg


class PhotoGallery(MediaGallery):
    def __init__(self):
        super().__init__("photo", xdg.photos_dir())

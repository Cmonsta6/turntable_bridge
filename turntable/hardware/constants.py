"""
Wire-level constants shared by the hardware layer.

Turntable protocol tokens, the image extensions we recognise, and the file
naming the capture path reproduces.
"""


IMAGE_EXTS = {".jpg", ".jpeg", ".tif", ".tiff", ".png", ".cr2", ".cr3",
              ".nef", ".arw", ".dng", ".pef", ".orf", ".rw2", ".raf"}

MSG_OK           = "CR+OK"
MSG_ERR          = "CR+ERR"
EVENT_DONE_TOKEN = "TB_END"

# The session counter is the LAST-used number: the next capture lands on
# counter + 1, so starting from 0 makes the first frame in every stack 0001.
# The resume and audit code both key off the resulting filenames.
SESSION_COUNTER_START = 0

# Returned by verify_connection() when the camera answers but will not report
# a model name. That is not the same as "no camera", so it gets its own value
# rather than being reported as a failure.
CAMERA_UNKNOWN = "connected (camera name unavailable)"

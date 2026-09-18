## v2.0

Unsigned exe: SmartScreen will warn once (More info → Run anyway). Settings carry over from v1.0.

**Live view**
- The preview now fills the window's full height, in the middle column, with the panels either side. Roughly 4x the picture area of v1.0 on a 1440p monitor.
- ⟳ button rotates the preview in 90° steps for a camera mounted on its side. Display only; saved photos are untouched. Remembered between sessions.
- The window switches between a landscape and a portrait layout to match, live and mid-run, with all settings kept.
- With no camera connected, a dashed outline shows where the frame will sit.
- Single shot button: one photo of what the preview shows, no table move, no focus move. Saved to `<subject>_singleshots/`, always to the PC.

**Files**
- Filenames now carry revolution, position and shot: `subject_rev-1_pos-007_shot-0002.NEF`, matching the folder they sit in.
- Recovering a v1.0 run keeps writing into its existing folders.

**Reliability**
- Fixed: an unplugged, powered-off or stuck turntable could freeze a run with Stop unable to end it. Serial writes now time out and go through the normal reconnect path.
- Stop pressed twice (or held four seconds) forces a stuck run to end. Recover carries on from the last checkpoint.
- Both Reconnect buttons work while a run is paused, without ending the run.

**Known limits**
- Full-size layout needs about 1700x930 px; smaller windows scale the whole interface down.
- On 1080p in portrait the frame sits inside a slightly wider panel. Nothing is cropped.

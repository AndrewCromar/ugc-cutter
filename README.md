# UGC Cutter

Cut phone footage on a simple timeline, then hand the cut points to the editor as a text file.

## Get it

Download from [the latest release](../../releases/tag/latest):

- **Windows:** `ugc-cutter.exe`. Windows may warn about an unknown app the first time: choose More info, then Run anyway. It takes a few seconds to start.
- **Linux:** `ugc-cutter`. Run `chmod +x ugc-cutter` once.

Every push to `main` builds and tests both, then replaces that release.

## Use it

1. Put your clips in a folder and click **Open folder**. They load in the order they were filmed, with a blue line at the end of each clip and everything green.
2. Find your cuts and mark what to throw away.
3. Click **Save**. This writes `cuts.txt` into the same folder. Open the folder again later and your cuts come back.

| Do this | To |
|---|---|
| Left click (or drag) on the timeline | Move the playhead |
| Space | Play or pause |
| Right click | Place a cut line |
| Right click on a cut line | Remove it |
| Shift + right click | Switch that section between green (keep) and red (cut) |
| Mouse wheel | Zoom in and out around the pointer |
| Shift + mouse wheel | Scroll sideways |
| Left / Right arrow | Move one frame |
| Shift + Left / Right arrow | Move one second |
| Ctrl+Z, Ctrl+Y | Undo, redo |
| Ctrl+S | Save |

**Skip red when playing** plays your cut as it will end up. It's off unless you tick it.

The app only ever adds `cuts.txt` to your folder. It never changes your clips.

## cuts.txt

Plain text with one line per section, in timeline order, separated by tabs. Times are seconds within that clip.

```
keep	Video Nov 02 2026, 3 06 55 PM.mov	5.800	8.800
cut	Video Nov 02 2026, 3 06 55 PM.mov	8.800	10.080
```

## Run from source

```
pip install -r requirements.txt
python run.py [folder]
python -m pytest tests      # add pytest first
```

## How playback stays fast

Phone HEVC video is slow to jump around in, and switching between files takes over a second. So when you open a folder, the app makes one small copy of the whole timeline in the background. The label next to Keeping shows its progress, and it takes over when ready. The copy only affects playback: cuts are saved against your original clips, and the copy is kept in your user cache folder, never next to your footage. All video reading runs in a separate worker process, so the window stays responsive.

## To do

- An Update button that installs the latest build from GitHub.
- An easy installer.

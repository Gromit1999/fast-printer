# Fast Printer

Print shipping labels from a PDF in two clicks. For Windows and Mac, with thermal label printers.

<!-- screenshot -->

## Download

| Computer | Download |
|---|---|
| Windows | **[Fast Printer.exe](https://github.com/Gromit1999/fast-printer/releases/latest/download/Fast-Printer.exe)** |
| Mac with an Apple chip (M1 or newer) | **[Fast Printer for Mac (Apple chip)](https://github.com/Gromit1999/fast-printer/releases/latest/download/Fast-Printer-Mac-Apple.zip)** |
| Older Mac with an Intel chip | **[Fast Printer for Mac (Intel)](https://github.com/Gromit1999/fast-printer/releases/latest/download/Fast-Printer-Mac-Intel.zip)** |

Not sure which Mac you have? Click the Apple menu, then **About This Mac**. It says "Chip: Apple M..." or "Processor: Intel".

### Windows

Double-click the .exe. Nothing to install.

If Windows says "Windows protected your PC", click **More info**, then **Run anyway**.

### Mac

1. Double-click the .zip to unzip it, then drag **Fast Printer** into your **Applications** folder.
2. Open it. The first time, your Mac will say it can't check the app is safe. Click **Done** (or **OK**).
3. Open **System Settings**, go to **Privacy & Security**, scroll down and click **Open Anyway** next to Fast Printer.
4. Open the app again and click **Open Anyway**. You only need to do this once.

**Updates:** when a new version is out, the Mac app asks if you want it when you open it. Click **Yes** and it updates itself and reopens. This works when the app is in your Applications folder.

## How to use

1. Drag a PDF onto the window, or click to find one.
2. Pick your label size.
3. Click **Print**.

## What it does

- **Finds your printers automatically.** Any printer set up on your computer shows in the list.
- **Built-in label sizes:** 100 x 150 mm and 60 x 30 mm. Add your own under **Label sizes**.
- **Set speed, darkness and copies.** It remembers them for each label size.
- **No gap setup.** The gap between labels is already set for you.
- **Multi-page PDFs** print one label per page, rotated to fit.

## Which printers work?

Thermal label printers that use TSPL, such as Munbyn, TSC, Xprinter, HPRT and iDPRT. Tested on a Munbyn ITPP130.

Your printer just needs to be set up on your computer once, using the maker's driver:

- **Windows:** install the driver from the printer maker's website.
- **Mac:** install the maker's Mac driver, then add the printer in **System Settings > Printers & Scanners**.

Zebra printers and normal office printers won't work.

## Something not printing right?

Click **Log** at the top of the app. It opens the folder with `log.txt` selected. Send that file to whoever is helping you. It records every print: the label size, settings, how much data was sent and what the printer said back.

## For developers

Run from source with Python 3.10 or newer:

```
pip install -r requirements.txt
python "Fast Printer.py"
python "Fast Printer.py" --selftest
```

To release: bump `APP_VERSION` in `Fast Printer.py`, then push a matching tag (`v1.2.0` for `APP_VERSION = "1.2.0"`). The build fails if they don't match, because the Mac app compares them to decide whether to update. Pushing the tag builds the Windows and Mac downloads on GitHub and publishes them as a release (see `.github/workflows/build.yml`).

## License

MIT. Free to use.

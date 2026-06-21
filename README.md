# 📚 Manga Organizer

A local web-based tool for organizing manga scans — split double-page spreads, edit images, and bundle chapters into volumes, all in one place.

![screenshot](screenshot.png)

---

## ✨ Features

- 📁 **Folder Scanning** — Automatically detects series/chapter folder structure
- ✂ **Page Splitting** — Split double-page spreads into two separate images
- 🖊 **Image Editing** — Crop, white mask, or black mask (single image or entire chapter at once)
- 📦 **Volume Bundling** — Bundle chapters into a folder, PDF, CBZ, or ZIP
- ✏ **Batch Rename** — Set prefix, number padding, and starting index
- 🔄 **Auto Backup & Restore** — Original files are automatically backed up before any edit

---

## 🖥 Requirements

- Windows / macOS / Linux
- Python 3.8+ (only if running from source)

---

## 💾 Download (Windows)

No installation needed — just download and run!

👉 **[Download manga_organizer.exe from Releases](../../releases/latest)**

A browser window will open automatically at `http://localhost:7777`.

---

## ⚙ Installation & Usage (from source)

**1. Install libraries** (optional — basic features work without them)

```bash
pip install Pillow       # Required for image editing features
pip install img2pdf      # For lossless PDF export
```

**2. Run**

```bash
python manga_organizer.py
```

A browser window will open automatically at `http://localhost:7777`.

---

## 📖 How to Use

### Opening a Folder
Type the path to your manga folder in the top input box, or click 📂 to browse.

### Supported Folder Structure
```
Series Name/
  ├── Volume 1 - Part 1/
  │     ├── 001.jpg
  │     └── 002.jpg
  └── Volume 1 - Part 2/
        └── 001.jpg
```

### Image Editing
**Double-click** any thumbnail to open the edit modal.

| Feature | Shortcut |
|---------|----------|
| Crop | `C` |
| White mask | `W` |
| Black mask | `B` |
| Apply to this image | `Ctrl+S` |
| Apply to entire chapter | `Ctrl+Shift+S` |
| Previous / Next image | `←` / `→` |
| Close | `Esc` |

### Page Splitting
1. Open the edit modal and click **✂ Split Line**
2. Drag the yellow line to adjust the split position
3. Choose direction: `L→R a|b` (Korean/Western) or `L←R b|a` (Japanese)
4. Click **✂ Split**

Split files (`005a.jpg`, `005b.jpg`) show an **↩ Restore Split** button to revert to the original.

### Volume Bundling
1. Select chapters in the left sidebar (Shift+click or drag)
2. Click **→ Add to Volume**
3. Choose output format and save

---

## 📁 Backup

Before any edit or split, the original file is automatically saved to a `_원본백업` folder in the same directory.
- Only saved once — existing backups are never overwritten
- Restore via the **↩ Restore** button in the edit modal

---

## 📄 License

MIT License — Free to use, modify, and distribute. Attribution appreciated.

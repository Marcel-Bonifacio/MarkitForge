# Supported formats

Generated from `markitforge/_formats.py` by `scripts/gen_formats_doc.py`. Run `markitforge --list-formats` to see what works on your machine.

| Format | Extensions | Install | Needs | Notes |
|---|---|---|---|---|
| PDF | `.pdf` | `markitforge[pdf]` |  | Tables, headings, page markers. --pdf-backend docling for hard layouts. |
| Word | `.docx` | `markitforge[docx]` |  | Math to LaTeX, headers/footers, page markers. |
| PowerPoint | `.pptx` | `markitforge[pptx]` |  | Per-slide sections, notes, charts, tables. |
| Excel | `.xlsx` | `markitforge[xlsx]` |  | Per-sheet sections; hidden sheets skipped by default. |
| Excel 97-2003 | `.xls` | `markitforge[xls]` |  |  |
| Legacy & OpenDocument | `.doc` `.rtf` `.odt` `.ott` `.wpd` `.ppt` `.odp` `.ods` | `markitforge[docx]` | soffice | Converted through LibreOffice (must be installed). |
| Outlook message | `.msg` | `markitforge[outlook]` |  |  |
| EPUB | `.epub` | core |  |  |
| HTML | `.html` `.htm` | core |  | Wikipedia, Bing SERP and RSS pages get special handling. |
| MHTML web archive | `.mht` `.mhtml` | core |  | Also OneNote / Word 'single file web page' exports. |
| CSV / TSV | `.csv` `.tsv` | core |  |  |
| JSON / XML / text | `.json` `.jsonl` `.xml` `.txt` `.md` | core |  |  |
| Jupyter notebook | `.ipynb` | core |  |  |
| RSS / Atom | `.rss` `.atom` | core |  |  |
| ZIP archive | `.zip` | core |  | Each file inside is converted. |
| Images | `.jpg` `.jpeg` `.png` | core | exiftool | EXIF metadata; descriptions with an LLM client. |
| Audio | `.wav` `.mp3` `.m4a` `.mp4` | `markitforge[audio-transcription]` | exiftool | Metadata plus speech transcript. |
| YouTube URL | `https://youtube.com/...` | `markitforge[youtube-transcription]` |  |  |
| Azure Document Intelligence | `(cloud)` | `markitforge[az-doc-intel]` |  | Use -d / --use-docintel. |

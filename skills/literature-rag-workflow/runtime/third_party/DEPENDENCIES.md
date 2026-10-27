# Dependency and license information

The separately installed dependencies below are not distributed in this skill
directory. Their publishers set their respective license terms; the project's
MIT license does not replace those terms. The versions listed for Python
packages are the direct version pins in `runtime/requirements.txt`, not a
complete lock of transitive dependencies or their licenses.

| Dependency | Version pin | License declared by publisher | Source |
| --- | --- | --- | --- |
| pypdf | 6.21.2 | BSD-3-Clause | [PyPI](https://pypi.org/project/pypdf/6.21.2/) |
| jsonschema | 4.26.0 | MIT | [PyPI](https://pypi.org/project/jsonschema/4.26.0/) |
| pdfplumber | 0.11.9 | MIT | [Upstream license](https://github.com/jsvine/pdfplumber/blob/stable/LICENSE.txt) |
| reportlab | 5.1.0 | BSD license | [PyPI](https://pypi.org/project/reportlab/5.1.0/) |
| pypdfium2 | 5.8.0 | Apache-2.0 / BSD-3-Clause; PDFium and other dependency licenses also apply | [PyPI licensing details](https://pypi.org/project/pypdfium2/5.8.0/) |

The pypdfium2 publisher states that PDFium is BSD-style licensed and that
binary distributions must carry the licenses for PDFium and the dependencies
included in the particular build. The publisher notes that some builds can
also link to libgcc. Installed wheels include build-specific license notices;
inspect the notices for the actual wheel or system build used. This repository
does not distribute those wheels or the PDFium binaries.

The PDF commands `pdfinfo`, `pdftotext`, and `pdftoppm` are provided by a
separately installed Poppler package. For example, Homebrew's Poppler formula
declares `GPL-2.0-only OR GPL-3.0-only`; see the [formula](https://formulae.brew.sh/formula/poppler)
and the [Poppler project](https://poppler.freedesktop.org/). Some PDFs require
separately installed [qpdf](https://qpdf.sourceforge.io/), whose [Homebrew
formula](https://formulae.brew.sh/formula/qpdf) declares Apache-2.0. Actual distributions, versions and their dependencies can
vary by installation and platform; inspect their own notices and licenses.

The bundled `academic-research-skills` files and their noncommercial terms are
documented in [`academic-research-skills/ORIGIN.md`](academic-research-skills/ORIGIN.md).
Neither this list nor the project's MIT license grants additional rights to
third-party components or settles the licensing obligations of a combined use.

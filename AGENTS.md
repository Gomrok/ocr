# Project instructions

- Preserve the existing OCR pipeline and user documents. Never commit uploads,
  generated outputs, API keys, private certificates, or production settings.
- Use the local Vazir Bold font and formal Persian copy. Do not add decorative
  icons or emoji to the product interface, progress messages, or exported text.
- Commit and push every change to `origin/main` before deploying it. Never deploy
  uncommitted changes or a revision that is not on `origin/main`.
- Run the relevant tests before pushing. Deploy with `deploy.ps1`, which checks
  the remote revision and stages an immutable release. Preserve previous releases.
- After deployment, verify HTTPS, health, and a real OCR upload/download through
  `https://ocr.gomrok.org` using a generated test document, not customer documents.
- Keep production secrets and uploaded files outside both Git and the IIS web root.

# FormSign

A small web app for sending forms to clients for e-signature and getting confirmation when they're done.

- **Form library**: upload PDF forms (engagement letters, consents, contracts) or build fillable forms (intake questionnaires, authorizations). PDFs can also carry extra fields for the client to fill in.
- **Signing packets**: bundle several forms for one client, in the order you choose, behind one private link.
- **Send**: copy the link and send it yourself, or have the app email it (optional SMTP).
- **Track and confirm**: each packet shows Sent → Opened → In progress → Signed, with a timestamped activity log. When the client finishes, they see a confirmation number and can download copies; you see "Confirmed" on the packet, and if email is on you both get the signed PDFs.
- **Signed PDFs**: the original pages, stamped with a signing footer, plus a signature certificate page (signature, name, time, IP address, device, SHA-256 fingerprint of the original file). A per-packet audit trail PDF is one click away.

Signatures are "simple electronic signatures" (drawn or typed, plus consent and an audit record). That's generally fine for engagement letters, intake and consent forms. For documents that legally need identity verification or a qualified signature, use a dedicated provider.

## SO# and sales rep

- Every packet has optional **SO#** and **Sales rep** fields (New packet page). Sales rep suggests names you've used before.
- Both show on the dashboard and packet page, and can be corrected on the packet page. The client sees the SO# on their packet page, and it's printed on the signed PDFs and the audit trail.
- **Dashboard search** finds packets by SO# (with or without the "SO" prefix), client name or email, or sales rep. The **All sales reps** menu narrows the list to one rep and combines with the search and the All / Open / Completed tabs.

## Layout and drawing approvals

On **New packet → Approvals**:

- **+ Layout approval (2 options)**: upload a picture for Option A and Option B (JPG, PNG, WebP or GIF) and optional notes. The client sees both side by side and must choose **Proceed with Option A**, **Proceed with Option B** or **Refuse both options** (a reason is required when refusing), then signs. Made for stone and seam layouts.
- **+ Drawing approval (PDF)**: upload the revised drawing, an optional revision label (e.g. Rev 3) and a note of what changed. The client reviews it and chooses **Approve the drawing as shown** or **Request changes** (what to change is required), then signs.
- The signed PDF shows the decision in a coloured box: for layouts both pictures with the chosen one highlighted; for drawings every page is stamped "APPROVED" or "CHANGES REQUESTED" with the client's name and date.
- Refusals and change requests are flagged in red on the packet page and on the dashboard so you know to send a revised version.
- Add up to 10 approvals per packet, alongside library forms and one-off files.

## One-off files and any file type

- **Form library** accepts any file type (PDF, Word, Excel, images, CAD, ZIP...), up to 50 MB.
- **New packet → One-off files**: drop in files just for this client. They're sent with that packet only and never saved to your library.
- For each one-off file, choose **Client signs this** or leave it unticked so the file is just for their records (they download it; you see when they did).
- PDFs are shown on screen and stamped with the signature. Other files are downloaded by the client to review; their signed record is a signature page with the file's name and SHA-256 fingerprint (images are shown on that page). The original file is kept with the packet and attached to the completion email.
- A packet with only files to review (nothing to sign) completes when the client clicks **Confirm I've received these files**.
- Only PDFs and common images open in the browser; every other type is always sent as a download, so an uploaded file can never run as a web page.

## Plumbing & appliance spec form

In **Form library**, click **Add plumbing & appliance spec form** once. Then include it in any packet like your other forms. The client adds rooms (up to 20), and in each room adds plumbing fixtures and appliances (sink, faucet, toilet, stove, dishwasher and more) with make and model, plus optional finish, quantity and notes. They sign at the bottom, and the signed PDF lists every room as a table. The item list is in `forms.py` (`CATEGORIES`).

## Run it on your computer (5 minutes)

Needs Python 3.10 or newer.

```bash
pip install -r requirements.txt
ADMIN_PASSWORD=choose-a-password BUSINESS_NAME="Your Company" python app.py
```

Open http://localhost:5000 and sign in. On Windows PowerShell set variables with `$env:ADMIN_PASSWORD="..."` first.

Locally, only you can open the signing links. To let clients sign, put it online.

## Put it online (Render, about US$7/month)

1. Create a free GitHub account and a new **private** repository; upload this folder's contents.
2. On [render.com](https://render.com) choose **New → Blueprint** and pick the repository. It reads `render.yaml`, which sets up the web service and a 1 GB persistent disk for your forms and signed documents.
3. When asked, fill in `ADMIN_PASSWORD`, `BUSINESS_NAME`, and `BASE_URL` (the address Render gives you, e.g. `https://formsign-xxxx.onrender.com`, or your own domain).
4. Deploy, open the address, sign in.

Any host that runs Docker works too (Railway, Fly.io, a VPS): build the `Dockerfile` and mount a persistent volume at `/data`.

**Keep the disk backed up.** Everything (database, uploaded forms, signed PDFs) lives in `DATA_DIR`. Render takes daily disk snapshots on paid plans; you can also download the folder periodically.

## Turn on email (optional)

Set these environment variables and restart. Any SMTP service works: Google Workspace, Microsoft 365, Postmark, SendGrid, Mailgun, Amazon SES.

| Variable | Example |
| --- | --- |
| `SMTP_HOST` | `smtp.postmarkapp.com` |
| `SMTP_PORT` | `587` (or `465` for SSL) |
| `SMTP_USER` / `SMTP_PASSWORD` | from your provider |
| `SMTP_FROM` | `Your Company <forms@yourdomain.com>` |
| `ADMIN_EMAIL` | where completion notices go, and the reply-to address |

With email on, you can send packets and reminders from the app, and both you and the client receive the signed PDFs when the packet is completed. Gmail accounts need an app password; for client-facing mail a transactional service (Postmark, SES) gives better delivery.

## All settings

| Variable | Required | Purpose |
| --- | --- | --- |
| `ADMIN_PASSWORD` | yes | Your sign-in password |
| `SECRET_KEY` | yes in production | Long random string that secures sessions |
| `BUSINESS_NAME` | recommended | Shown to clients and on PDFs |
| `BASE_URL` | recommended | Public address, used in emailed links |
| `DATA_DIR` | no | Storage folder (default `./data`) |
| `LINK_EXPIRY_DAYS` | no | Default link lifetime, also used by "Extend link" (default 30) |

## How it works

- `app.py`: Flask routes for the admin side (`/`, `/library`, `/packets/...`) and the client side (`/s/<token>/...`).
- `pdfgen.py`: builds signed PDFs and audit trails with pypdf and ReportLab.
- `mailer.py`: optional SMTP email.
- SQLite database with four tables: `templates`, `packets`, `packet_items`, `events`.
- When a packet is created, each form is snapshotted into it, so editing or removing a form later never changes what a client already received or signed.
- Signing links use 32-character random tokens, expire, and can be cancelled. Anyone with a link can sign it, so send links only to the client.

## Ideas for later

- Place the signature on a specific spot on the PDF page instead of a certificate page
- Multiple signers per packet (e.g. two company directors), signing order
- Automatic reminder emails after N days
- Multiple staff logins
- Client-uploaded attachments (ID, documents)

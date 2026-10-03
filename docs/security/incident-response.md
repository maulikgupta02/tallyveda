# Personal data breach response

How we handle a suspected breach of data held by Tally Connector (books of account shared by
MSMEs, consent records, logins). Written to meet the DPDP Rules 2025 breach-intimation duty
(in force from 13 May 2027) and what lending partners expect today. Review it with counsel.

## Roles

- **Incident lead:** the platform operator (whoever holds the `/admin` platform-admin login).
  They run the response, decide on notifications and keep the record.
- **Grievance contact:** the address on `/privacy` (`TC_GRIEVANCE_EMAIL`). Breach questions
  from MSMEs and partners come in here.

## What counts

Any unauthorised access, disclosure, alteration, loss or destruction of data we hold, or loss
of access to it. Examples: a leaked admin password, a bank seeing another bank's applicant, a
database credential exposed, a lost laptop holding exports, an infrastructure provider's breach
that includes our data.

## Steps

1. **Contain, within hours.**
   - Rotate whatever leaked: user passwords (`/admin/users`), the database password (Neon
     console, then `DATABASE_URL` on Render), the Render API key, SMTP password.
   - Disable affected users or suspend the affected bank (`/admin/banks`).
   - Stop daily updates for affected companies if their tokens may be exposed
     (`/admin/msmes/{id}`); the connector removes itself on its next check.
2. **Assess.** Use `/admin/audit`, Render logs and Neon logs to find what was accessed, which
   companies and banks are affected, and since when. Keep notes with timestamps.
3. **Notify the Data Protection Board** without delay with a first report (what happened, when,
   likely impact), and a detailed report **within 72 hours** of becoming aware: facts, cause,
   affected data and people, measures taken, and contact details. Use the Board's online
   process once available.
4. **Notify affected people and partners** without delay, in plain language: what happened, the
   likely consequences, what we have done, what they can do (e.g. change passwords), and the
   grievance contact. For MSMEs, email the `contact_email` on each affected request; for banks,
   their admin users. Contracts with banks may set shorter deadlines: check them.
5. **Fix and record.** Fix the cause, then write up the incident (timeline, impact, actions,
   lessons) and keep it with the notifications sent.

## Keep ready

- An export of `/admin/audit` and the list of banks and contacts (store outside the platform).
- Credentials for Render, Neon and the SMTP account, so they can be rotated quickly.

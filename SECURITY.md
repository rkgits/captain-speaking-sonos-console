# Security Policy

Captain Speaking is a small local-network app, but security reports are welcome and taken seriously.

## Supported versions

| Version | Supported |
|---|---|
| 1.1.x | Yes |
| 1.0.x and older | Please upgrade to the latest release |

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Report it privately instead:

1. Go to the **Security** tab of this repository.
2. Choose **Report a vulnerability**.
3. Describe what you found and how to reproduce it.

You can also contact the maintainer, [@rkgits](https://github.com/rkgits), through GitHub.

This is a hobby project maintained by one person. You can expect an acknowledgement within about a week, and I'll keep you updated until it is fixed or I explain why it won't be. With your permission, I'll credit you in the release notes.

## What is in scope

- Bugs that let one device on your network do more than the app is meant to allow
- Ways to read or write files outside the app's own folders (for example through the recordings or artwork routes)
- Command or code injection, or unsafe handling of data from speakers or the network
- Anything that exposes private information from the computer running the app

## Known limitations (not vulnerabilities)

- **The app has no login by design.** Anyone who can reach its port can control your speakers and play audio through them. Run it only on a trusted home network and **never expose it to the internet**.
- Announcements are served over plain HTTP, because Sonos speakers cannot use the app's self-signed HTTPS certificate.

## Staying safe

- Keep the port (8787 by default) closed on your router. Do not forward it.
- Keep Python and the app's packages up to date. Dependabot alerts are enabled on this repository.

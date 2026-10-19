"""Rubika messenger surface.

* ``rubika_client``  Rubika SDK wrapper (pinned ``rubpy==7.3.5``): login,
                     contacts/channels, forward/send-text, discovery helpers.
* ``login_code``     300s mirror of a logged-in account's in-app messages
                     into the owner's log group (for manual re-login).
* ``features``       in-process Rubika automations: PV secretary, channel
                     report, reply responder, group-reply, profile sync.
* ``brain_control``  isolated stop/pause controller used by the Brain modes
                     (number file splitter + add + send).
* ``pdf_export``     PV photo archive → PDF (reportlab + Pillow).
"""

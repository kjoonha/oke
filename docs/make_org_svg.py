"""Build docs/oke-org.svg: the OKe org chart with the band's disco characters.

usage: python docs/make_org_svg.py .
"""
import io
import os
import re
import sys

REPO = sys.argv[1]
band = io.open(os.path.join(REPO, "docs", "oke-band.svg"), encoding="utf-8").read()
groups = re.findall(r'<g class="f (f\d)" style="animation-delay:([-\d.]+)s">(.*?)</g>', band, re.S)
# disco row: 4 roles x 4 frames; each frame's pixels span x 92..158, y 36..84 around (125, 60)
ROLE_FRAMES = {r: groups[i * 4:(i + 1) * 4] for i, r in enumerate(("orchestrator", "pm", "worker", "reviewer"))}
SRC = {"orchestrator": 125, "pm": 221, "worker": 317, "reviewer": 413}

COLOR = {"orchestrator": "#b79cff", "pm": "#7fb2ff", "worker": "#7ee0a1", "reviewer": "#ffb86b"}
W, H = 920, 630


def character(role, cx, cy, k, delay=0.0):
    out = []
    for f, d, body in ROLE_FRAMES[role]:
        out.append('<g class="f %s" style="animation-delay:%.2fs">%s</g>' % (f, float(d) - delay, body))
    return ('<g transform="translate(%d %d) scale(%.2f) translate(%d -60)">%s</g>'
            % (cx, cy, k, -SRC[role], "".join(out)))


def card(role, cx, top, w, h, title, sub, k, delay=0.0):
    c = COLOR[role]
    x = cx - w // 2
    return "".join([
        '<rect x="%d" y="%d" width="%d" height="%d" rx="14" fill="url(#card)" stroke="%s" stroke-width="2" filter="url(#glow-%s)"/>'
        % (x, top, w, h, c, role),
        '<rect x="%d" y="%d" width="%d" height="4" rx="2" fill="%s" opacity=".9"/>' % (x + 18, top, w - 36, c),
        character(role, cx, top + 12 + int(30 * k), k, delay),
        '<text x="%d" y="%d" text-anchor="middle" class="role">%s</text>' % (cx, top + h - 28, title),
        '<text x="%d" y="%d" text-anchor="middle" class="sub">%s</text>' % (cx, top + h - 12, sub),
    ])


def link(x1, y1, x2, y2, color, ident):
    my = (y1 + y2) / 2
    d = "M%d %d C%d %.0f %d %.0f %d %d" % (x1, y1, x1, my, x2, my, x2, y2)
    return ('<path id="%s" d="%s" class="track"/><path d="%s" class="flow" stroke="%s"/>' % (ident, d, d, color))


def dot(path, color, dur, begin):
    return ('<circle r="4.5" fill="%s"><animateMotion dur="%ss" begin="%ss" repeatCount="indefinite" '
            'keyPoints="1;0" keyTimes="0;1" calcMode="linear"><mpath href="#%s"/></animateMotion></circle>'
            % (color, dur, begin, path))


# layout
ORCH = (460, 104, 230, 150)               # cx, top, w, h
PMS = [(250, 306), (670, 306)]            # cx, top (w 190, h 124)
KIDS = [(150, "worker"), (350, "reviewer"), (570, "worker"), (770, "reviewer")]
KID_TOP, KID_W, KID_H = 470, 160, 118

parts = []
parts.append('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" role="img" '
             'aria-label="OKe org chart: you talk to the orchestrator; it runs one PM per repository; each PM runs a '
             'worker and a reviewer. Assignments flow down, reports flow up.">' % (W, H, W, H))
parts.append("""<style>
.f{opacity:0;animation:f 1.6s steps(1,end) infinite}@keyframes f{0%{opacity:1}25%{opacity:0}100%{opacity:0}}
text{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;fill:#f1eefb}
.role{font-size:15px;font-weight:700;letter-spacing:.02em}
.sub{font-size:11px;fill:#9a94b3}
.note{font-size:11.5px;fill:#8a85a3}
.track{fill:none;stroke:#2f2942;stroke-width:3}
.flow{fill:none;stroke-width:2.5;stroke-linecap:round;stroke-dasharray:2 10;animation:down 1.1s linear infinite}
@keyframes down{to{stroke-dashoffset:-12}}
.blink{animation:blink 1.1s steps(1,end) infinite}@keyframes blink{50%{opacity:0}}
@media (prefers-reduced-motion:reduce){.f,.flow,.blink{animation:none}.f0{opacity:1}}
</style>""")
parts.append('<defs>'
             '<radialGradient id="bg" cx="50%" cy="0%" r="90%"><stop offset="0" stop-color="#241d3a"/><stop offset="1" stop-color="#110e19"/></radialGradient>'
             '<linearGradient id="card" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#221d31"/><stop offset="1" stop-color="#18141f"/></linearGradient>'
             '<pattern id="dots" width="22" height="22" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r="1" fill="#2a2440"/></pattern>'
             + "".join('<filter id="glow-%s" x="-30%%" y="-30%%" width="160%%" height="160%%"><feDropShadow dx="0" dy="0" stdDeviation="7" flood-color="%s" flood-opacity=".35"/></filter>' % (r, c) for r, c in COLOR.items())
             + '</defs>')
parts.append('<rect width="%d" height="%d" rx="18" fill="url(#bg)"/><rect width="%d" height="%d" rx="18" fill="url(#dots)"/>' % (W, H, W, H))

# links first, under the cards
ocx, otop, ow, oh = ORCH
parts.append(link(460, 70, 460, otop, COLOR["orchestrator"], "u-o"))
for i, (pcx, ptop) in enumerate(PMS):
    parts.append(link(ocx, otop + oh, pcx, ptop, COLOR["orchestrator"], "o-p%d" % i))
for i, (kcx, role) in enumerate(KIDS):
    pcx, ptop = PMS[i // 2]
    parts.append(link(pcx, ptop + 124, kcx, KID_TOP, COLOR["pm"], "p-k%d" % i))
parts.append(dot("p-k1", COLOR["reviewer"], 2.4, 0))
parts.append(dot("p-k2", COLOR["worker"], 2.4, 0.9))
parts.append(dot("o-p0", COLOR["pm"], 2.6, 1.5))
parts.append(dot("o-p1", COLOR["pm"], 2.6, 0.3))
parts.append(dot("u-o", COLOR["orchestrator"], 1.4, 0.6))

# you
parts.append('<rect x="395" y="26" width="130" height="44" rx="22" fill="#f1eefb"/>'
             '<text x="440" y="54" text-anchor="middle" style="font-size:16px;font-weight:700;fill:#14111d">❯ you</text>'
             '<rect x="487" y="40" width="9" height="17" fill="#7a4fd1" class="blink"/>')
parts.append('<text x="548" y="44" class="note">one conversation</text><text x="548" y="59" class="note">questions come back as popups</text>')

parts.append(card("orchestrator", ocx, otop, ow, oh, "orchestrator", "interviews · queues · asks you", 1.2))
parts.append('<text x="%d" y="%d" class="note">your Claude Code session</text>' % (ocx + ow // 2 + 18, otop + 64))
parts.append('<text x="%d" y="%d" class="note">plans and merges need your OK</text>' % (ocx + ow // 2 + 18, otop + 80))
for i, (pcx, ptop) in enumerate(PMS):
    parts.append(card("pm", pcx, ptop, 190, 124, "PM", "repo-%s · plans · merges" % "ab"[i], 1.0, 0.4 * i))
parts.append('<text x="460" y="374" text-anchor="middle" class="note">one PM per repository</text>')
for i, (kcx, role) in enumerate(KIDS):
    sub = "own pane + git worktree" if role == "worker" else "checks the commits"
    parts.append(card(role, kcx, KID_TOP, KID_W, KID_H, role, sub, 0.9, 0.2 * i))

parts.append('<text x="460" y="%d" text-anchor="middle" class="note">dashes: assignments going down · dots: reports coming back up</text>' % (H - 14))
parts.append("</svg>")
svg = "\n".join(parts)
io.open(os.path.join(REPO, "docs", "oke-org.svg"), "w", encoding="utf-8", newline="\n").write(svg)
print(len(svg))

"""PDF layout experiment: what structure can be recovered before any business logic?

The question this folder answers is narrow and practical: given the ~184k PDFs the
hackathon ships, can we recover *fields* (labels, values, table cells) without OCR and
without hand-writing one parser per bank/entity? And if yes, can the layouts themselves
tell us the rules (per company, per period) instead of us guessing them?
"""

"""dayloop.aggregate — estimator: raw ActivityRecords -> DayTimeline.

timeline.build orchestrates the four sources, segment turns screen records
into Sessions, redact scrubs secrets/PII before anything is stored or sent
to an LLM.
"""

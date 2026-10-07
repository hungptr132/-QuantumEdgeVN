"""Test support: builders that produce real documents for the pipeline to read.

Nothing here reimplements production logic. A support module may only *produce input*
(a file on disk, a row of scaffolding) or *read output*; the moment one of them computes
something the pipeline also computes, a test can pass while both are wrong together.
"""

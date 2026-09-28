"""Diagram runtime: agents drawn as blocks, attachments and flow wires, validated before they run.

    diagram = load_file("diagrams/incident-brief.json")
    problems = validate(diagram, mode="sample")
    result = run(diagram, Context(...), payload)
"""
from . import evals
from .diagram import Diagram, DiagramError, load, load_file
from .executor import Context, DiagramInvalid, StepFailed, resume, run
from .validator import Violation, validate

__all__ = ['evals', 'Context', 'Diagram', 'DiagramError', 'DiagramInvalid', 'StepFailed', 'Violation',
           'load', 'load_file', 'resume', 'run', 'validate']

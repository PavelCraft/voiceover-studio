from django.utils.translation import gettext as _

from voiceover_core.timing import split_script

from ..models import PhraseTimingProject


def initialize_phrase_timing_project(project):
    """Prepare standalone and workflow timing projects through one path."""
    if project.mode == PhraseTimingProject.MODE_AUTOMATIC:
        project.processing_status = "pending"
        project.processing_step = "preparing"
        project.phrases = []
    else:
        project.phrases = [
            {"id": index, "text": text, "start": None, "end": None}
            for index, text in enumerate(split_script(project.source_text), 1)
        ]
        if not project.phrases:
            raise ValueError(str(_("The text contains no phrases.")))
        project.processing_status = "complete"
        project.processing_step = "complete"
    project.save()
    return project.mode == PhraseTimingProject.MODE_AUTOMATIC

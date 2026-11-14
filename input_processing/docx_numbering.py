"""Materialize common Word automatic list labels so clause identifiers are not lost."""
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from input_processing.schemas.processed_contract import JobError


def _format(number, kind):
    if kind == "decimal":
        return str(number)
    if kind == "decimalZero":
        return f"{number:02d}"
    if kind in ("lowerLetter", "upperLetter"):
        value = ""
        while number > 0:
            number, remainder = divmod(number - 1, 26)
            value = chr(65 + remainder) + value
        return value.lower() if kind == "lowerLetter" else value
    if kind in ("lowerRoman", "upperRoman"):
        value = ""
        for amount, label in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"),
                              (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
            repeats, number = divmod(number, amount)
            value += label * repeats
        return value.lower() if kind == "lowerRoman" else value
    raise JobError(f"Unsupported Word numbering format: {kind}; convert to PDF")


class DocxNumbering:
    def __init__(self, document):
        try:
            self.root = document.part.part_related_by(RT.NUMBERING).element
        except KeyError:
            self.root = None  # Valid DOCX files without lists may omit numbering.xml.
        self.counters, self.cached = {}, {}

    def prefix(self, paragraph):
        if paragraph._p in self.cached:
            return self.cached[paragraph._p]
        properties = paragraph._p.pPr
        num_pr = properties.numPr if properties is not None else None
        style = paragraph.style
        seen = set()
        while num_pr is None and style is not None and style.style_id not in seen:
            seen.add(style.style_id)
            properties = style.element.pPr
            num_pr = properties.numPr if properties is not None else None
            style = style.base_style
        if num_pr is None or num_pr.numId is None or num_pr.numId.val == 0:
            return ""
        number_id = str(num_pr.numId.val)
        level = num_pr.ilvl.val if num_pr.ilvl is not None else 0
        if self.root is None:
            raise JobError("Missing DOCX numbering part")
        nums = [item for item in self.root.findall(qn("w:num")) if item.get(qn("w:numId")) == number_id]
        if not nums:
            raise JobError("Missing DOCX numbering definition")
        abstract_id = nums[0].find(qn("w:abstractNumId")).get(qn("w:val"))
        abstract = [item for item in self.root.findall(qn("w:abstractNum")) if item.get(qn("w:abstractNumId")) == abstract_id]
        if not abstract:
            raise JobError("Missing DOCX abstract numbering definition")
        definitions = {int(item.get(qn("w:ilvl"))): item for item in abstract[0].findall(qn("w:lvl"))}
        starts = {}
        for override in nums[0].findall(qn("w:lvlOverride")):
            index = int(override.get(qn("w:ilvl")))
            if override.find(qn("w:lvl")) is not None:
                definitions[index] = override.find(qn("w:lvl"))
            if override.find(qn("w:startOverride")) is not None:
                starts[index] = int(override.find(qn("w:startOverride")).get(qn("w:val")))

        def attribute(index, name, default):
            definition = definitions.get(index)
            if definition is None:
                raise JobError("Missing DOCX list level")
            item = definition.find(qn("w:" + name))
            return item.get(qn("w:val")) if item is not None else default

        state = self.counters.setdefault(number_id, {})
        start = starts.get(level, int(attribute(level, "start", "1")))
        state[level] = state.get(level, start - 1) + 1
        for deeper in list(state):
            if deeper > level:
                # Respect explicit restart suppression (lvlRestart=0).
                restart = int(attribute(deeper, "lvlRestart", str(deeper)))
                if restart and level < restart:
                    del state[deeper]
        kind = attribute(level, "numFmt", "decimal")
        template = attribute(level, "lvlText", f"%{level + 1}.")
        if kind == "bullet":
            prefix = "• "
        elif kind == "none":
            prefix = ""
        else:
            for index in range(level + 1):
                placeholder = f"%{index + 1}"
                if placeholder in template:
                    number = state.get(index, starts.get(index, int(attribute(index, "start", "1"))))
                    template = template.replace(placeholder, _format(number, attribute(index, "numFmt", "decimal")))
            prefix = template + " "
        self.cached[paragraph._p] = prefix
        return prefix

"""Bounded lexical context for follow-up questions; history supplies no facts."""

import re

from .store import search_terms


def expand_query(query):
    return re.sub(r"\bз\s*/\s*п\b|\bзп\b|\bзарплат[а-яё]*\b",
                  "зарплата заработная плата аванс", query, flags=re.I)


def retrieval_query(question, history=()):
    current = expand_query(question)
    terms = search_terms(current)
    follow_up = (len(terms) <= 2 or bool(re.search(
        r"^(?:а|и|тогда|ещё|еще)\b|\b(?:это|этого|этой|него|неё|нее|там)\b", question, re.I)))
    if not history or not follow_up:
        return current
    previous = [item["content"][:1000] for item in history if item["role"] == "user"][-4:]
    if not previous:
        return current
    # A new amount replaces the previous one. Without a new amount, retain the
    # last selected amount so «а сколько соуса?» keeps the 250 g recipe.
    context = " ".join(previous)
    amounts = re.findall(r"\b\d+(?:[.,]\d+)?\s*(?:грамм\w*|гр\.?|г\b|кг\b)?", context, flags=re.I)
    context = re.sub(r"\b\d+(?:[.,]\d+)?\s*(?:грамм\w*|гр\.?|г\b|кг\b)?", "", context, flags=re.I)
    if amounts and not re.search(r"\d", question):
        context += " " + amounts[-1]
    return current + " " + expand_query(context)

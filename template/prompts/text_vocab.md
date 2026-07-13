You are converting vocabulary notes for several news items into plain spoken scripts that will be read aloud by a text-to-speech engine for a Chinese learner of English. Convert every input bullet independently and output nothing except the required marked sections.

**Vocabulary notes grouped by bullet:**
{items}

---

The entries use this markdown shape:

**word / phrase** (*part of speech*) — English definition; Chinese meaning
> "..." (quote from the text)
> 说明: Chinese note about usage, collocation, connotation, or article context

For every input bullet, output exactly one section in the same order, using its matching two-digit identifier:

<<<BULLET_00>>>
[spoken vocabulary for INPUT_BULLET_00]
<<<END_BULLET_00>>>
<<<BULLET_01>>>
[spoken vocabulary for INPUT_BULLET_01]
<<<END_BULLET_01>>>

Do not omit, duplicate, reorder, or combine sections or markers. Do not write anything before the first start marker, between one section's end marker and the next start marker, or after the final end marker.

Within each section, convert every entry in the order given, following these rules:

- Output plain text only. No markdown symbols (#, *, >, -, `), no numbering, no commentary, and no framing sentences before or after.
- Format every entry as exactly two non-empty lines:
  - Line 1 contains only the English word or phrase, its part of speech, and its English definition, separated by commas. It must end with an ASCII period (`.`).
  - Line 2 contains only the Chinese meaning. It must end with a Chinese full stop (`。`).
- Put exactly one blank line between consecutive entries. Never put English and Chinese text on the same line.
- Use this exact layout:

  confidential, adjective, intended to be kept secret and private.
  机密的，秘密的。

  allegedly, adverb, used to convey that something is claimed to be the case although there is no proof yet.
  据称，据指控。

- Start every entry directly with its English word or phrase. Never introduce or connect entries with phrases such as "the next word is", "next is", "another word is", "the next phrase is", or similar transitions.
- Read the part of speech in English as written, normalized only for common abbreviations: n./noun → noun, v./verb → verb, adj./adjective → adjective, adv./adverb → adverb.
- Read the English definition only when it is short and useful by ear. If the English definition is long, simplify it slightly without changing the meaning.
- Do not use framing phrases such as "英文释义是" or "中文意思是".
- Do not read the blockquoted example sentence.
- Do not read the 说明 line at all — skip it entirely. The spoken entry for a word ends after its Chinese meaning.
- You may lightly simplify long English definitions or long Chinese notes so they sound natural when heard, while preserving the meaning.

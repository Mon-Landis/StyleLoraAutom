"""R1 A–J exact rules plus conservative diagnostics; never mutate annotations."""
from dataclasses import dataclass, field
import re
from stylelora.tagging.normalization import normalize_tag
from stylelora.tagging.tokenizer import tokenize_prompt, syntax_of
from stylelora.project_db.validation import digest
from stylelora.project_db.errors import ValidationError

# Explicit versioned vocabulary, independent of skill files at runtime.
WORDS = {
 'A': 'artist name|signature|watermark|twitter username|pixiv username|instagram username|patreon username|weibo username|web address|username|artist logo|style parody',
 'B': 'masterpiece|best quality|high quality|good quality|normal quality|amazing quality|great quality|worst quality|low quality|bad quality|very aesthetic|aesthetic|displeasing|very displeasing|beautiful|absurdres|incredibly absurdres|highres|lowres|hires|8k|4k|uhd|hdr|ultra detailed|extremely detailed|highly detailed|detailed|intricate details|sharp focus|newest|recent|mid|early|oldest|general|sensitive|questionable|explicit|safe',
 'C': 'oil painting|watercolor|acrylic paint|gouache|ink|ink wash|pastel|crayon|colored pencil|charcoal|traditional media|marker (medium)|copic (medium)|pencil sketch|pencil (medium)|ballpoint pen (medium)|graphite (medium)|fountain pen (medium)|brush (medium)|airbrush (medium)|paper (medium)|canvas (medium)|painting (medium)|digital media|digital painting|digital art|photo|photograph|3d|3d render|cg|cgi|render|blender (medium)|mmd|illustration|artwork|concept art|clay (medium)|paper cutout|collage|knitting (medium)|lego (medium)',
 'D': 'lineart|line art|no lineart|thick outlines|thin outlines|outline|sketch lines|sketch|rough sketch|linework|bold lines|clean lines|colored lineart|monoline|flat color|flat colors|cel shading|anime coloring|soft shading|hard shadows|gradient|gradient shading|painterly|impasto|blending|airbrushed|textured|grainy|monochrome|greyscale|grayscale|limited palette|muted colors|pastel colors|vibrant colors|colorful|spot color|partially colored|sepia|duotone|film grain|halftone|screentones|screentone|dithering|noise|grain|paper texture|canvas texture|scanlines|pixel art|vector trace|vector art|low poly|voxel|chromatic aberration|cross-hatching|hatching|stippling|ambient occlusion|ray tracing|subsurface scattering|realistic shading',
 'E': 'retro artstyle|retro|vintage|oldschool|old-school|nostalgic|realistic|photorealistic|semi-realistic|hyperrealistic|super deformed|deformed|stylized|simple style|toon (style)|cartoon|comic|manga|western comics (style)|american comic|webtoon|anime screencap|anime style|anime|manhwa|doujin|minimalism|art nouveau|art deco|ukiyo-e|sumi-e|impressionism|expressionism|cubism|surreal|surrealism|abstract|pop art|psychedelic|vaporwave|synthwave|lofi|kawaii|ghibli|disney|pixar',
 'F': 'blurry|blurry background|blurry foreground|blur|bokeh|depth of field|shallow depth of field|soft focus|out of focus|tilt shift|defocus|lens flare|vignetting|vignette|fisheye|wide angle|telephoto|macro|light leaks|anamorphic|glow effect|sparkle effect|sunbeam filter|glitch|vhs artifacts|jpeg artifacts|compression artifacts|scan artifacts|pixelated|distortion|motion blur|speed lines|motion lines|emphasis lines|zoom layer|high contrast|low contrast|oversaturated|desaturated|saturated|color filter|tinted|warm filter|cool filter|black and white|vivid|dramatic lighting|cinematic|cinematic lighting|cinematic composition|volumetric lighting',
 'H': 'official art|promotional art|game cg|event cg|official style|third-party edit|cover|cover page|scan|screencap|wallpaper|poster|magazine scan|bad id|bad pixiv id|bad twitter id|bad link|md5 mismatch|resolution mismatch|source smaller|artist request|character request|copyright request|commentary|commentary request|english commentary|translation request|translated|partially translated|check translation|revision|duplicate|alternate version|speech bubble|thought bubble|text|english text|chinese text|japanese text|korean text|text focus|copyright name|logo|dated|border|letterboxed|pillarboxed|comic panel|multiple views|split screen|sample|character name|stats|landscape orientation|portrait orientation|square orientation|vertical|horizontal|vertical orientation|horizontal orientation|vertical composition|horizontal composition|vertical image|horizontal image|tall image|wide image|square image|square composition|aspect ratio|widescreen|wide screen|landscape|square',
}
SHORT = frozenset('mid early recent general safe sensitive explicit cover sample text border landscape square vertical horizontal'.split())
EXCEPTIONS = frozenset(('gradient hair','pastel pink dress','stained glass window','holding paintbrush','easel','holding photo',
                        'border collie','gothic architecture','steampunk goggles','vertical stripes','horizontal stripes','square pupils'))
AMBIGUOUS = frozenset(('chibi','gothic','steampunk','cyberpunk','frame'))


@dataclass(frozen=True)
class BlocklistSnapshot:
    style_tags: frozenset[str]
    rules_hash: str
    catalog_hash: str
    mode: str = 'warn'
    artists: frozenset[str] = frozenset()
    known: frozenset[str] = frozenset()
    unverifiable: tuple[str, ...] = ()
    policy: str = 'style-content-v1'


@dataclass(frozen=True)
class Hit:
    tag: str | None
    nl_span: tuple[int, int] | None
    category: str
    reason: str
    certainty: str = 'exact'
    image_id: int = 0
    action: str = 'warn'


def build_blocklist(style_prompt, catalog, *, mode='warn', profile=None):
    from stylelora.modes.profiles import get_profile
    profile = profile or get_profile()
    if not profile.enabled or profile.blocklist_policy != 'style-content-v1':
        raise ValidationError('profile:unavailable')
    if mode not in ('warn','remove'):
        raise ValidationError('blocklist:invalid_mode')
    tokens = tokenize_prompt(style_prompt,catalog=catalog)
    styles = frozenset(t.normalized for t in tokens if t.normalized and t.syntax == 'plain')
    entries = getattr(catalog,'entries',{})
    aliases = getattr(catalog,'aliases',{})
    artists = {k for k,v in entries.items() if v == 'artist'}
    artists.update(a for a,k in aliases.items() if k in artists)
    return BlocklistSnapshot(styles,digest(WORDS),catalog.revision,mode,frozenset(artists),
                             frozenset(entries)|frozenset(aliases)|frozenset(getattr(catalog,'translations',{})),
                             tuple(t.raw for t in tokens if t.syntax != 'plain'))


def _classify(key, snapshot):
    if snapshot.policy != 'style-content-v1':
        raise ValidationError('profile:unavailable')
    if key in snapshot.style_tags:
        return 'style_prompt','exact'
    if key in snapshot.artists or re.match(r'(?:@|artist:|by |art by |drawn by |illustrated by |in the style of |inspired by )',key):
        return 'A','exact'
    if key in EXCEPTIONS:
        return None
    if key in AMBIGUOUS:
        return 'J','review_required'
    if re.search(r'\((?:medium|style|artist)\)$',key):
        return 'C' if key.endswith('(medium)') else 'A','exact'
    if syntax_of(key,known=key in snapshot.known) != 'plain' or re.match(r'(?:score \d|source anime)',key):
        return 'G','exact'
    if re.match(r'(?:rating:|year \d{4}$)',key):
        return 'B','exact'
    if re.fullmatch(r'\d+\s*[x:]\s*\d+',key):
        return 'H','exact'
    for category, words in WORDS.items():
        for word in words.split('|'):
            if key == word or word not in SHORT and re.search(r'(?<!\w)'+re.escape(word)+r'(?!\w)',key):
                return category,'exact'
    if re.search(r'brushstrokes?|rendering|artstyle|atmosphere|(?:painted|drawn) in',key):
        return 'J','review_required'
    return None


def check_blocklist(tags, nl, snapshot):
    if snapshot.policy != 'style-content-v1':
        raise ValidationError('profile:unavailable')
    result = []
    for tag in tags:
        key = normalize_tag(tag.tag)
        found = _classify(key,snapshot)
        if found:
            result.append(Hit(key,None,found[0],'blocklist:hit',found[1]))
    if nl:
        text = nl.lower().replace('_',' ')
        vocabulary = set(snapshot.style_tags)|set(snapshot.artists)
        for words in WORDS.values():
            vocabulary.update(w for w in words.split('|') if w not in SHORT)
        vocabulary.update(('brushstrokes','rendering','dreamy atmosphere','painted in','in the style of'))
        protected = [m.span() for word in EXCEPTIONS for m in re.finditer(r'(?<!\w)'+re.escape(word)+r'(?!\w)',text)]
        seen = set()
        for word in sorted(vocabulary):
            for match in re.finditer(r'(?<!\w)'+re.escape(word)+r'(?!\w)',text):
                span = match.span()
                if span in seen or any(a <= span[0] and span[1] <= b for a,b in protected):
                    continue
                seen.add(span)
                result.append(Hit(None,span,'NL','blocklist:nl_review_required','review_required'))
    return tuple(result)

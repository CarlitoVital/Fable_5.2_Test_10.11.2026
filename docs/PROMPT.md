# The prompt, rewritten

The original request was:

> Goal a Animation of the development of la paz similar to the one in my fotos of hong kong –
> out of a insta reel. Look for sites which provide valide data and so on.

Below is a version that would have produced this repository with far fewer round trips. It
states the deliverable, describes the reference instead of only pointing at it, says what
"valid data" means, and fixes the decisions an agent would otherwise have to guess.

---

## Goal

Make a short vertical video that shows how **La Paz and El Alto (Bolivia) grew, building by
building**, in the style of the attached Instagram reel (tulparmaps, "Day 9 – Hong Kong
buildings construction year").

## The reference – what to keep

The three screenshots show a 4:5 portrait frame with:

- a dark, hill-shaded 3D landscape seen from an oblique angle, filling the whole frame;
- buildings as extruded 3D blocks that appear in the year they were built, coloured on a
  ramp from deep red (old) through orange to pale peach (new); undated buildings in grey;
- top left: a small letter-spaced kicker and a heavy sans-serif title;
- bottom left: a large year counter in the ramp colour, a running building count, and a
  histogram "buildings completed per year" with a playhead;
- two footer lines: a methods note, then author and data credits.

Keep that layout and mood. Improve on it where you can (the reel's own comments complain
that the map is upside down – keep north up).

## Data – find it, test it, and be honest about it

1. **Research first.** List the open datasets that exist for (a) building footprints,
   (b) year of construction or settlement age, (c) building heights, (d) terrain and
   (e) land cover. For each: publisher, licence, resolution, time span, download URL and
   known weaknesses. Prefer official or peer-reviewed sources.
2. **No invented data.** If La Paz has no open cadastre with construction years, say so and
   use satellite-derived settlement age instead – and label it as that in the video
   ("year = first satellite detection"), not as construction dates.
3. **Test the data before trusting it.** Compare sources with each other and with
   neighbourhoods whose history is known (the colonial centre must not come out as 1990s;
   El Alto's first estates such as Villa Dolores and Ciudad Satélite are older than 1985;
   Senkata and Ventilla are not). Reject what fails and write down why.
4. **Undated stays undated.** Buildings without evidence are shown separately in grey and
   counted, never silently guessed.
5. The timeline starts where the evidence starts. Do not extrapolate backwards to make the
   story longer.

## Deliverables

1. `out/*.mp4` – 1080 × 1350 px, 30 fps, about 30 s, H.264, ready for Instagram – plus a
   poster frame.
2. A reproducible pipeline in Python: one command to fetch, one to process, one to render.
   Raw data is downloaded from the public sources, not committed.
3. One config file for everything visual (camera, colours, timing, texts) and one for the
   data (area, sources, thresholds).
4. A README with the source table, the dating method in plain language, the checks you
   ran, the limitations, and how to point the pipeline at another city.

## Visual spec

- Area: the La Paz – El Alto conurbation, from the altiplano rim to Zona Sur.
- Camera: north up, oblique, the whole built-up area in frame; a slow pull-out is welcome.
- Start with what already stood in the first year of data, then one beat per year; new
  buildings rise and glow briefly; finish on a hold of the complete city.
- Building heights from data, vertically exaggerated enough to read at city scale.
- Typeface: Montserrat or similar. Text must stay legible on a phone.
- Add two or three place labels for orientation (El Alto, La Paz, Zona Sur) with their
  elevation – the 800 m drop is the point of this city.

## Constraints

- Must render on a machine **without a GPU** and without paid services or API keys.
- Only open data with a licence that allows publishing the result; credit every source in
  the frame.
- Keep the repository small: code, fonts, the final video and a summary of the numbers.

## How to work

Work in stages and show me a still or a table at each one before going on:
(1) source table, (2) top-down diagnostic map of building years, (3) one frame of the 3D
look, (4) a low-resolution preview, (5) the final render.
Ask me before decisions that change the story (start year, area, what counts as a
building). Decide the rest yourself and note it in the README.

## Done means

- [ ] Every number on screen can be traced to a source file and a rule in the code.
- [ ] The known-neighbourhood checks pass and are documented.
- [ ] The video plays on a phone, the lettering is readable, nothing is cut off at the edges.
- [ ] `README.md` lets someone else rerun it and tells them what not to conclude from it.

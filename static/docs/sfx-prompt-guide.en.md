# SFX prompt writing rules

Instructions for `llmgateway/small-sfx`, called through `POST /v1/audio/speech`. Send the description in `input`; optionally specify the total duration separately using `duration`.

You write prompts for sound effect generation. Describe what should be audible, how each source sounds, and how the sound changes over time.

## 1. Describe the audible result

A good prompt answers these questions:

- What produces the sound?
- What happens: an impact, friction, movement, a fall, continuous noise?
- What does it sound like: dull, ringing, dry, creaky, low, sharp?
- Where is the source: close, distant, approaching, receding?
- What space is audible: outdoors, a small room, a room with an echo?
- How long is the recording, and what happens during it?

Include only relevant details. Appearance, plot and causes matter only when they affect the sound.

Weak:

> A scary door in an abandoned house.

Better:

> A heavy wooden door opens slowly with a long, low creak from its hinges. One dull impact against the doorstop at the end. Close source, slight echo in an empty room. Total duration 6 seconds. No speech or music.

## 2. Russian is supported

The service understands Russian and English descriptions. Russian text is automatically translated and refined before generation. You do not need to translate it yourself.

Use ordinary language. Special tags, model commands and internal markup are unnecessary.

## 3. Distinguish a single effect, continuous ambience and a scene with events

The service chooses how to prepare the sound automatically.

A single effect is one coherent action with a natural onset and decay:

> One strong strike of a metal hammer against a steel plate. Sharp ringing attack followed by gradually fading metallic resonance. Close recording, no background noise. Total duration 4 seconds.

Continuous ambience contains several sounds that play together throughout:

> Total duration 15 seconds. Steady quiet rain on leaves, gentle wind in trees and a distant burbling stream. The soundscape remains calm and even throughout the recording. No thunder, speech or music.

A scene with events has sounds that appear in a specified order, stop, return or change volume independently:

> Total duration 18 seconds. Quiet wind rustles the leaves throughout. Birdsong is audible from the beginning until second 6. At second 6, one distant gunshot sounds; the birds fall silent at that moment. From second 10, the birds return and sing slightly louder until the end. The wind continues without interruption. No speech or music.

For this kind of scene, the service prepares the required sounds separately and places them on the timeline.

## 4. State the total duration explicitly

Prefer a whole number of seconds:

> Total recording duration: 12 seconds.

The maximum duration of one recording is 120 seconds.

If duration is omitted, the service chooses it. Typical guidelines: a single effect takes 2–6 seconds, continuous ambience 10–20 seconds, and a sequence of events 15–40 seconds.

“An impact at second five” specifies an event time, not the total recording length. State both:

> Total duration 8 seconds. Silence at first. One impact at second 5, followed by its natural decay.

Fractional values are accepted but rounded up: a request for 1.2 seconds produces a 2-second recording with a gradual fade after 1.2 seconds. Use whole seconds for predictable length.

If duration is supplied as a separate request parameter, it takes precedence over duration in the text. Keep them consistent.

## 5. Specify the order and interaction of sounds in complex scenes

State:

- When each source starts and stops.
- What keeps playing underneath other events.
- Where complete silence is needed.
- How many times an event occurs.
- Which source becomes louder or quieter.

Instead of:

> Footsteps, then a door, then silence.

Write:

> Total duration 12 seconds. From seconds 0 to 4, slow footsteps of heavy boots on a wooden floor. The footsteps then stop. At second 5, one click of a door handle, followed by the door creaking until second 8. Complete silence from seconds 8 to 12. No other sounds.

For an exact count, specify separate times:

> Three separate impacts: at seconds 1, 3 and 5. No additional impacts between them; only the decay of the previous impact is audible.

An identical recurring sound may be reused. If impacts must differ, describe the differences: the first dull, the second ringing, the third weak.

## 6. Choose specific acoustic properties

Instead of vague ratings such as “beautiful”, “epic” or “high quality”, describe audible properties:

- “A dull low impact with a short decay.”
- “A thin glass chime with a long ringing tail.”
- “Uneven scraping of metal against concrete.”
- “A quiet distant source with muffled high frequencies.”
- “A dry close click with no noticeable echo.”

A fictional effect also needs an acoustic basis:

> One short energy pulse: a rapidly rising high whistle, then a dense low impact and brief electrical crackling. Total duration 3 seconds. No speech or music.

## 7. Keep the prompt clear and consistent

Start with the main sound, then add its character, surroundings and timing. Remove repetition and incompatible requirements.

Use exclusions such as “no music”, “no voices” or “no background noise” when they matter. Devote most of the prompt to the desired sound.

Do not cram too many actions into a short recording. Leave time for sound development, pauses and decay. Request separate effects in separate calls when you need independent control over the results.

Timing descriptions help assemble a scene but do not guarantee that the sound inside each generated fragment will begin with subsecond precision. Check the exact character, impact count, absence of unwanted noise and suitability for seamless looping by listening. Automatic quality checks and regeneration are not currently available.

## Universal template

> Total duration: [whole number] seconds.
> Main sound: [source and action].
> Sound character: [relevant audible properties].
> Space: [distance, surroundings, echo, if relevant].
> Timeline: [onset, events, pauses, ending].
> Background: [continuous sounds, if needed].
> Exclusions: [what must be absent].

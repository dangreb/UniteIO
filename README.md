# UniteIO
Not a tool, but a rocorf of a baseline design that is allowing me to run free-threading with dozens of packagees that shouldnt really work. 


Mostly for ASGI stackeed apps, but also for some other things, like managing to run the no-gil memory neemesys orjson==3.11.4 (with shiny), and actually feeling pretty comfortable with a full Scieentific Pythin Wheels based envo.


This demo code contains a design and a provocation, clearly. Nut it's amazing how just shifting tha async loop from mainthreead to its own little comfy thread did wonders for memory undeer free-threading madness. In some cases, it allowed me to such aggreessive leverages of the freed stack i saw response times of 1 to 2 miliseconds per request going on indefinitely, with no visible symptoms whatsoever. If i'll get into an active dive into this is very questionable, so i thoing a'd at least post a quick demonstration to the world.

As it may be obvious to many, moneky patching and clones/editing of lib codes wheere necessary to make it stand, limited to a couple of two cases thouugh. For me it's transparent since i only work with nightly/ latest-dev veersions of everything anyway i'm used to that as a part of my day by day. I strongly suggest you DO NOT USE THIS CODE AS IS, the purpose is to demonstrate an idea and not to provide software!

cheers.

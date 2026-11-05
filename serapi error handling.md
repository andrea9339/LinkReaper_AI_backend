| SerpAPI response | Backend behavior |
|---|---|
| status: "Success" with organic results | Collect results; continue. |
| status: "Success" with empty/missing organic_results | Continue searching remaining pages and queries. |
| status: "Success" with an error saying Google returned no results | Continue. This is case we previously fixed. |
| status: "Error" | Stop entire search, regardless of organic results. |
| Missing status, but nonempty error | Stop entire search. |

# Live Token Use and Credit Overlay
I want to have a live view of my API token usage and remaining creadits on an overlay on my machine as a collapsable tab that will constantly reside on my Desktop.

I want to be able to run a script called tokens, that either launches this small desktop overlay or re-open it if it has already been launched. 

The sources I want to pull usage AND Credit data from are:
	-  Google AI Studio at https://aistudio.google.com/usage?timeRange=last-28-days
	- Google Cloud Console at https://console.cloud.google.com/welcome/new?organizationId=149572111417&project=gemini-api-505408
	- Claude Platform https://platform.claude.com/usage
	- OpenAi Platform https://platform.openai.com/settings/organization/usage

Have this overlay relay the live token use and remaing credits from all of theses sources.

The credentials required to login will be configured in my ~/.bashrc.

Put an instruction set on how to use this script with any of the required tags or configurations (in a ~/.bashrc or a local .env) into a README.md file.



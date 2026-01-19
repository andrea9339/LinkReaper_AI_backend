# This is the **backend** developement for the new linkreaper with AI filtering

## Enviroment variables
The APIs used are protected with  ***environment variables***
I create an enviromental variable  to secure serpapi and openai on the web
```
api_key = os.getenv('SERPAPI_KEY')
openai_api_key = os.getenv('OPENAI_API_KEY')
```

And put the key-value on the Render environment variables.

Then I deployed my flask app on a cloud hosting platform (render)

## Requirments issues

In the deployment process usually the problem is with the **requirements.txt**, specifically compatibility between th packages and python version in render.

## Gunicorn settings

I’ve put the gunicorn code line since gunicorn provides us the WSGI server(you have to put the name of the app in the folder). So, if the name in the folder is like “**Linkreaper**.py”, you should write:
```
gunicorn Linkreaper:app
```

## Update frontend URL

after succefully deploying the flask app on render, I finally have the link of my web app, so to connect it to my frontend, I’ve simply replaced the link: 

```https://linkreaper-deploy.onrender.com/api/:path*```

instead of the local host:

```http://localhost:5000/api/:path*```

in my ***next.config.ts*** file in the frontend folder.

## Update backend URL

once I’ve deployed the frontend on vercel,
I’ve updated in my flask app the CORS origin to match new Vercel app URL:
```
CORS(app, resources={r"/*": {"origins": "https://link-reaper.vercel.app/"}})
```
And push on render again to the updated commit.


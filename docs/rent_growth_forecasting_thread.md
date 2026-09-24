# Rent Growth Forecasting: A Practitioner's Note

*Draft Twitter thread — May 2026*

---

**1/**
Been building rent growth forecasting models for our multifamily underwriting. Thought I'd share what actually works, what doesn't, and why you should be skeptical of anyone claiming to "predict" rents.

I'm an owner/operator with skin in the game — not selling a product.

**2/**
We tested 6 approaches on quarterly CoStar submarket data (~100 obs):

- Naive (last quarter's YoY = next quarter's)
- ARIMA (univariate time series)
- Bayesian VAR (rent + vacancy + absorption + deliveries)
- Ridge regression
- ElasticNet
- Gradient boosting (LightGBM)

**3/**
Backtest results (1-step-ahead, expanding window):

| Model | RMSE | Beats Naive? |
|-------|------|--------------|
| ElasticNet | 2.37% | p=0.007 |
| Ridge | 2.43% | p=0.009 |
| BVAR | 2.44% | p=0.009 |
| GBM | 3.07% | p=0.22 |
| ARIMA | 3.28% | p=0.94 |
| Naive | 3.29% | — |

Only 3 of 5 models statistically beat "just use last quarter."

**4/**
What I learned:

ARIMA is overrated for rent forecasting. It captures momentum but has no economic content — can't see vacancy spikes or supply waves coming. Strong at 1-2 quarters, useless at 8.

GBM overfits badly on small quarterly datasets. We had to neuter it (shallow trees, heavy subsampling) just to make it usable.

**5/**
BVAR is the workhorse. It models the system: rent <-> vacancy <-> absorption <-> deliveries. Minnesota prior shrinkage keeps it from going haywire on 100 observations.

But it assumes linear shock propagation. At regime breaks (COVID, rate spike), the cross-equation elasticities flip and it's temporarily wrong.

**6/**
Ridge/ElasticNet are boring and reliable. Regularization handles the collinearity that kills OLS on real estate data. Fast, interpretable, rarely catastrophically wrong.

If I had to pick one model it'd be Ridge. But ensembling beats any single model.

**7/**
Our ensemble: inverse-RMSE weighting. Models that backtest better get more weight. Simple, robust, no overfitting the weights themselves.

We explicitly exclude Naive from the ensemble — it's a benchmark, not a contributor.

**8/**
Now the important part: why you shouldn't trust any of this too much.

These models explain ~30-40% of rent variance. The other 60%+ is stuff we can't measure or predict: migration shifts, employer relocations, zoning changes, capital markets sentiment.

**9/**
The models assume relationships stay stable. They don't.

Vacancy -> rent elasticity flipped in 2022. Pre-COVID a 200bp vacancy spike meant -50bp rent growth. Post-COVID the same spike meant -150bp. The model learned the old regime.

**10/**
We also can't model what we can't see:

- Shadow pipeline (entitled but not started)
- Competitive pressure from adjacent submarkets
- Concession burn-off timing
- Operator execution quality

CoStar often disagrees with our models. Sometimes they're seeing supply data we aren't.

**11/**
So why bother?

Because the alternative is vibes. At least this gives us:
- Explicit assumptions we can stress test
- Error bands calibrated to backtest
- A structured way to ask "what would need to change for CoStar to be right?"

**12/**
My actual use: I run base/bull/bear scenarios calibrated to backtest RMSE. The point forecast is less important than understanding the range.

If the deal doesn't work in the bear case, I don't do the deal. The model doesn't tell me what will happen — it tells me what could.

**13/**
One more thing: thin markets are humbling.

In a 4,000-unit submarket, a single 150-unit delivery moves vacancy 4% and rent growth 100+ bps. Your model's confidence interval should be wider than you'd like.

**14/**
TL;DR:
- Regularized regressions and BVAR beat fancier ML on small RE datasets
- Ensemble > any single model
- Backtest everything, trust nothing at face value
- Models are thinking tools, not crystal balls
- If you have skin in the game, you learn humility fast

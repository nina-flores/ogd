# ogd

# 2/5/2025
Things to do:
* Apply the function to all countries to get any exposure (within county, outside country, and offshore wells)
    ** There are 6 steps in the function. By moving from sub-national boundaries, to national boundaries we now get through steps 1-3 fine. I am not running into issues on step 4 where the union occurs. I think this may have to do with memory issues since I do not run into this issue when applying this to a subset of the points. I edited the step 4 function to reduce memory usage and will check in the morning how it is doing. Other ways to get at memory issues are running one country at a time, which we will need to do anyways for the outside country analyses. Leads us to other to-do's.
* Another thing I have done so far to reduce the size of of the country shapefile is buffering the countries by 10km (just being overly cautious) and then filtering the original country boundaries to those with overlap of a well. Reduced it down to like 40. We can use this filtered version for some of the next steps. Adding the r file where I did this in case its helpful for thinking about some of the other steps.


* Create country-specific parquet files. 
* Create a parquet file that masks to water.
* Create country-specific buffered files. 
* Explore what the sub-national issue is, see if this is still present when restricting just to countries with wells. Since we may need this for the second aim.

* Apply the function to get within country well exposure (use country specific parquet).
* Apply the function to get the neighboring country well exposure (use country-specific buffered files and then mask it with country specific file).
* Apply the function to get the offshore well exposure (use water masked).


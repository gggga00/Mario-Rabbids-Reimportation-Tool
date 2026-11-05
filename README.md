# Notes:

This tool is built to reimport romFS files into the sdf archive format used by Mario+Rabbids: Kingdom Battle.

It supports reimporting modified romFS file, as well as entirely new files. 
Make sure folder and filenames for new files are not too funky, as the structure of the archive format only allows latin characters.

For modified dds files the code will ignore the header entirely and assume it to be the same as the original. This means you can reimport them both with and without the snowdrop header extension just fine.
If you want to use a dds file with a different header use an entirely new dds file instead.


# Usage: 

1. Make a folder "import", where you place all the fully extracted/modified and new romFS files you want to reimport.
For existing files make sure to preserve the extractions folder structure, within "import".

2. Put "import", the executable (or python script), sdf.sdftoc and all the sdfdata files you want to modifiy into the same folder/place.
If you also want to add entirely new files make sure to include sdf-C-2999.sdfdata, as that is where no files will be stored in

3. Run the executable/python script.
The modified sdf files will be placed inside a folder "out".
